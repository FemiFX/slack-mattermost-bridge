"""The routing rules: loops, threading, files, edits, deletes, reactions."""
import json

import pytest

from conftest import mm_post, slack_event
from mmbridge.bridge import normalise_emoji


# -- the basic crossing -------------------------------------------------------

def test_a_slack_message_reaches_mattermost(bridge):
    bridge.handle_slack_event(slack_event("hello *there*"))
    assert len(bridge.mattermost.posted) == 1
    posted = bridge.mattermost.posted[0]
    assert posted["message"] == "hello **there**"
    assert posted["channel_id"] == "M1"


def test_a_mattermost_message_reaches_slack(bridge):
    bridge.handle_mattermost_event(mm_post("hello **there**"))
    assert len(bridge.slack.posted) == 1
    assert bridge.slack.posted[0]["text"] == "hello *there*"


def test_the_author_is_named_with_a_provenance_marker(bridge):
    bridge.handle_slack_event(slack_event())
    assert bridge.mattermost.posted[0]["username"] == "slack-U1 (via Slack)"
    bridge.handle_mattermost_event(mm_post())
    assert bridge.slack.posted[0]["username"] == "mm-M1 (via Mattermost)"


def test_identity_override_off_prefixes_the_name_instead(config):
    from conftest import FakeMattermost, FakeSlack
    from mmbridge.bridge import Bridge

    config.identity_override = False
    bridge = Bridge(config, slack=FakeSlack(), mattermost=FakeMattermost())
    try:
        bridge.handle_slack_event(slack_event("hi"))
        posted = bridge.mattermost.posted[0]
        assert posted["username"] == ""
        assert posted["message"].startswith("**slack-U1 (via Slack):**")
    finally:
        bridge.store.close()


def test_a_message_outside_the_allowlist_is_ignored(bridge):
    bridge.handle_slack_event(slack_event(channel="C-OTHER"))
    bridge.handle_mattermost_event(mm_post(channel_id="M-OTHER"))
    assert bridge.mattermost.posted == []
    assert bridge.slack.posted == []


# -- loop prevention ----------------------------------------------------------

def test_the_bridges_own_slack_post_does_not_come_back(bridge):
    """The decisive guard: a timestamp already in the link table is our own mirror."""
    bridge.handle_mattermost_event(mm_post("from mattermost"))
    mirrored_ts = bridge.slack.posted[0]["ts"]

    # Slack now reports that very message as a new channel event.
    bridge.handle_slack_event(slack_event("from mattermost", ts=mirrored_ts, user="U2"))

    assert len(bridge.mattermost.posted) == 0, "a mirrored message was echoed back"


def test_a_post_authored_by_our_own_bot_is_ignored(bridge):
    bridge.handle_slack_event(slack_event(user=bridge.slack.bot_user_id))
    assert bridge.mattermost.posted == []


def test_a_post_carrying_our_bot_id_is_ignored(bridge):
    bridge.handle_slack_event(slack_event(user="", bot_id=bridge.slack.bot_id, subtype="bot_message"))
    assert bridge.mattermost.posted == []


def test_the_bridges_own_mattermost_post_does_not_come_back(bridge):
    bridge.handle_slack_event(slack_event("from slack"))
    mirrored_id = bridge.mattermost.posted[0]["id"]
    bridge.handle_mattermost_event(mm_post("from slack", post_id=mirrored_id, user_id="M2"))
    assert len(bridge.slack.posted) == 0


def test_a_mattermost_post_by_the_bot_account_is_ignored(bridge):
    bridge.handle_mattermost_event(mm_post(user_id=bridge.mattermost.bot_user_id))
    assert bridge.slack.posted == []


def test_a_redelivered_slack_event_is_only_mirrored_once(bridge):
    event = slack_event("only once")
    bridge.handle_slack_event(event)
    bridge.handle_slack_event(event)
    assert len(bridge.mattermost.posted) == 1


def test_system_posts_are_not_mirrored(bridge):
    bridge.handle_mattermost_event(mm_post("x joined the channel", type="system_join_channel"))
    assert bridge.slack.posted == []


def test_join_and_leave_subtypes_are_not_mirrored(bridge):
    bridge.handle_slack_event(slack_event("joined", subtype="channel_join"))
    assert bridge.mattermost.posted == []


# -- threading ----------------------------------------------------------------

def test_a_slack_thread_reply_lands_in_the_matching_mattermost_thread(bridge):
    bridge.handle_slack_event(slack_event("root", ts="100.1"))
    root_post_id = bridge.mattermost.posted[0]["id"]

    bridge.handle_slack_event(slack_event("reply", ts="100.2", thread_ts="100.1"))

    assert bridge.mattermost.posted[1]["root_id"] == root_post_id


def test_a_mattermost_thread_reply_lands_in_the_matching_slack_thread(bridge):
    bridge.handle_mattermost_event(mm_post("root", post_id="mm1"))
    root_ts = bridge.slack.posted[0]["ts"]

    bridge.handle_mattermost_event(mm_post("reply", post_id="mm2", root_id="mm1"))

    assert bridge.slack.posted[1]["thread_ts"] == root_ts


def test_a_reply_whose_root_was_never_bridged_becomes_a_new_root(bridge):
    """Losing the thread shape beats losing the message."""
    bridge.handle_slack_event(slack_event("orphan reply", ts="100.2", thread_ts="099.0"))
    assert len(bridge.mattermost.posted) == 1
    assert bridge.mattermost.posted[0]["root_id"] is None


# -- files --------------------------------------------------------------------

def test_a_slack_file_is_uploaded_to_mattermost_and_attached(bridge):
    bridge.handle_slack_event(slack_event("see this", files=[{"id": "F1", "name": "report.pdf"}]))
    assert bridge.mattermost.uploaded[0]["filename"] == "report.pdf"
    assert bridge.mattermost.posted[0]["file_ids"] == ["mmfile1"]


def test_a_mattermost_file_is_uploaded_under_the_authors_slack_message(bridge):
    bridge.handle_mattermost_event(mm_post("see this", file_ids=["F1"]))
    message_ts = bridge.slack.posted[0]["ts"]
    upload = bridge.slack.uploads[0]
    assert upload["files"] == [("F1.pdf", b"mm-bytes")]
    assert upload["thread_ts"] == message_ts


def test_a_mattermost_file_with_no_text_still_gets_an_anchor_message(bridge):
    bridge.handle_mattermost_event(mm_post("", file_ids=["F1"]))
    assert bridge.slack.posted[0]["text"] == "_F1.pdf_"


def test_files_are_not_transferred_when_the_feature_is_off(bridge):
    bridge.config.sync_files = False
    bridge.handle_slack_event(slack_event("see this", files=[{"id": "F1", "name": "r.pdf"}]))
    assert bridge.mattermost.uploaded == []
    assert bridge.mattermost.posted[0]["file_ids"] is None


def test_an_empty_message_with_no_files_is_dropped(bridge):
    bridge.handle_slack_event(slack_event(""))
    assert bridge.mattermost.posted == []


# -- edits and deletes --------------------------------------------------------

def test_a_slack_edit_updates_the_mattermost_post(bridge):
    bridge.handle_slack_event(slack_event("before", ts="100.1"))
    post_id = bridge.mattermost.posted[0]["id"]

    bridge.handle_slack_event({"event_id": "Ev2", "event": {
        "type": "message", "subtype": "message_changed", "channel": "C1",
        "message": {"ts": "100.1", "text": "after", "user": "U1"},
        "previous_message": {"ts": "100.1", "text": "before"},
    }})

    assert bridge.mattermost.updated == [{"id": post_id, "message": "after"}]


def test_an_edit_that_changes_nothing_is_not_propagated(bridge):
    bridge.handle_slack_event(slack_event("same", ts="100.1"))
    bridge.handle_slack_event({"event_id": "Ev2", "event": {
        "type": "message", "subtype": "message_changed", "channel": "C1",
        "message": {"ts": "100.1", "text": "same", "user": "U1"},
        "previous_message": {"ts": "100.1", "text": "same"},
    }})
    assert bridge.mattermost.updated == []


def test_a_mattermost_edit_updates_the_slack_message(bridge):
    bridge.handle_mattermost_event(mm_post("before", post_id="mm1"))
    ts = bridge.slack.posted[0]["ts"]

    bridge.handle_mattermost_event({"event": "post_edited", "data": {"post": json.dumps(
        {"id": "mm1", "channel_id": "M1", "user_id": "M1", "message": "after", "type": ""}
    )}})

    assert bridge.slack.updated == [{"channel": "C1", "ts": ts, "text": "after"}]


def test_editing_a_mirrored_message_does_not_edit_back(bridge):
    """An edit only travels in the direction the message originally travelled."""
    bridge.handle_slack_event(slack_event("from slack", ts="100.1"))
    mirrored_id = bridge.mattermost.posted[0]["id"]

    bridge.handle_mattermost_event({"event": "post_edited", "data": {"post": json.dumps(
        {"id": mirrored_id, "channel_id": "M1", "user_id": "M2", "message": "tampered", "type": ""}
    )}})

    assert bridge.slack.updated == []


def test_a_slack_delete_removes_the_mattermost_post_and_the_link(bridge):
    bridge.handle_slack_event(slack_event("doomed", ts="100.1"))
    post_id = bridge.mattermost.posted[0]["id"]

    bridge.handle_slack_event({"event_id": "Ev3", "event": {
        "type": "message", "subtype": "message_deleted", "channel": "C1", "deleted_ts": "100.1",
    }})

    assert bridge.mattermost.deleted == [post_id]
    assert bridge.store.link_by_slack("C1", "100.1") is None


def test_a_mattermost_delete_removes_the_slack_message(bridge):
    bridge.handle_mattermost_event(mm_post("doomed", post_id="mm1"))
    ts = bridge.slack.posted[0]["ts"]

    bridge.handle_mattermost_event({"event": "post_deleted", "data": {"post": json.dumps(
        {"id": "mm1", "channel_id": "M1", "user_id": "M1", "message": "doomed", "type": ""}
    )}})

    assert bridge.slack.deleted == [{"channel": "C1", "ts": ts}]


def test_edits_and_deletes_respect_their_feature_switches(bridge):
    bridge.config.sync_edits = False
    bridge.config.sync_deletes = False
    bridge.handle_slack_event(slack_event("x", ts="100.1"))
    bridge.handle_slack_event({"event_id": "E", "event": {
        "type": "message", "subtype": "message_changed", "channel": "C1",
        "message": {"ts": "100.1", "text": "y", "user": "U1"},
        "previous_message": {"ts": "100.1", "text": "x"}}})
    bridge.handle_slack_event({"event_id": "E2", "event": {
        "type": "message", "subtype": "message_deleted", "channel": "C1", "deleted_ts": "100.1"}})
    assert bridge.mattermost.updated == []
    assert bridge.mattermost.deleted == []


# -- reactions ----------------------------------------------------------------

def test_a_slack_reaction_reaches_the_mattermost_post(bridge):
    bridge.handle_slack_event(slack_event("react to me", ts="100.1"))
    post_id = bridge.mattermost.posted[0]["id"]

    bridge.handle_slack_event({"event_id": "Ev4", "event": {
        "type": "reaction_added", "user": "U2", "reaction": "thumbsup",
        "item": {"type": "message", "channel": "C1", "ts": "100.1"},
    }})

    assert bridge.mattermost.reactions_added == [(post_id, "thumbsup")]


def test_removing_a_slack_reaction_removes_it_in_mattermost(bridge):
    bridge.handle_slack_event(slack_event("react", ts="100.1"))
    post_id = bridge.mattermost.posted[0]["id"]
    bridge.handle_slack_event({"event_id": "Ev5", "event": {
        "type": "reaction_removed", "user": "U2", "reaction": "thumbsup",
        "item": {"type": "message", "channel": "C1", "ts": "100.1"}}})
    assert bridge.mattermost.reactions_removed == [(post_id, "thumbsup")]


def test_a_mattermost_reaction_reaches_the_slack_message(bridge):
    bridge.handle_mattermost_event(mm_post("react", post_id="mm1"))
    ts = bridge.slack.posted[0]["ts"]

    bridge.handle_mattermost_event({"event": "reaction_added", "data": {"reaction": json.dumps(
        {"user_id": "M2", "post_id": "mm1", "emoji_name": "tada"}
    )}})

    assert bridge.slack.reactions_added == [("C1", ts, "tada")]


def test_our_own_reaction_is_not_echoed(bridge):
    bridge.handle_mattermost_event(mm_post("react", post_id="mm1"))
    bridge.handle_mattermost_event({"event": "reaction_added", "data": {"reaction": json.dumps(
        {"user_id": bridge.mattermost.bot_user_id, "post_id": "mm1", "emoji_name": "tada"}
    )}})
    assert bridge.slack.reactions_added == []


def test_a_reaction_on_an_unbridged_message_is_ignored(bridge):
    bridge.handle_slack_event({"event_id": "Ev6", "event": {
        "type": "reaction_added", "user": "U2", "reaction": "x",
        "item": {"type": "message", "channel": "C1", "ts": "999.9"}}})
    assert bridge.mattermost.reactions_added == []


@pytest.mark.parametrize("raw,expected", [
    ("thumbsup", "thumbsup"),
    ("thumbsup::skin-tone-4", "thumbsup"),
    (":tada:", "tada"),
    ("", ""),
])
def test_emoji_modifiers_are_stripped(raw, expected):
    assert normalise_emoji(raw) == expected


# -- direction switches -------------------------------------------------------

def test_one_way_slack_to_mattermost_ignores_mattermost(bridge):
    bridge.config.direction = "slack_to_mm"
    bridge.handle_mattermost_event(mm_post("should not travel"))
    bridge.handle_slack_event(slack_event("should travel"))
    assert bridge.slack.posted == []
    assert len(bridge.mattermost.posted) == 1


def test_one_way_mattermost_to_slack_ignores_slack(bridge):
    bridge.config.direction = "mm_to_slack"
    bridge.handle_slack_event(slack_event("should not travel"))
    bridge.handle_mattermost_event(mm_post("should travel"))
    assert bridge.mattermost.posted == []
    assert len(bridge.slack.posted) == 1


# -- Mattermost bot-account identity limitation -------------------------------

def test_a_bot_account_falls_back_to_a_name_prefix_in_mattermost(config):
    """Mattermost ignores override_username from a Bot Account.

    It stamps from_bot on every such post and renders the bot's own identity, so
    the author's name would silently vanish. Prefixing keeps attribution.
    """
    from conftest import FakeMattermost, FakeSlack
    from mmbridge.bridge import Bridge

    bridge = Bridge(config, slack=FakeSlack(), mattermost=FakeMattermost(is_bot_account=True))
    try:
        assert bridge.config.identity_override is True
        assert bridge.mattermost_identity_override is False

        bridge.handle_slack_event(slack_event("hi"))
        posted = bridge.mattermost.posted[0]
        assert posted["username"] == ""
        assert posted["message"].startswith("**slack-U1 (via Slack):**")
    finally:
        bridge.store.close()


def test_a_bot_account_does_not_degrade_the_slack_direction(config):
    """Slack honours chat:write.customize regardless of the Mattermost account."""
    from conftest import FakeMattermost, FakeSlack
    from mmbridge.bridge import Bridge

    bridge = Bridge(config, slack=FakeSlack(), mattermost=FakeMattermost(is_bot_account=True))
    try:
        assert bridge.slack_identity_override is True
        bridge.handle_mattermost_event(mm_post("hi"))
        assert bridge.slack.posted[0]["username"] == "mm-M1 (via Mattermost)"
    finally:
        bridge.store.close()


def test_an_ordinary_user_account_keeps_the_mattermost_override(bridge):
    assert bridge.mattermost_identity_override is True
    bridge.handle_slack_event(slack_event("hi"))
    assert bridge.mattermost.posted[0]["username"] == "slack-U1 (via Slack)"


# -- Slack file style ---------------------------------------------------------

def _inline_bridge(config):
    from conftest import FakeMattermost, FakeSlack
    from mmbridge.bridge import Bridge

    config.slack_file_style = "inline"
    return Bridge(config, slack=FakeSlack(), mattermost=FakeMattermost())


def test_inline_style_sends_one_message_carrying_the_file(config):
    """Slack strips the username override from any message a file is attached to,
    so inline style names the author in the text instead."""
    bridge = _inline_bridge(config)
    try:
        bridge.handle_mattermost_event(mm_post("here you go", file_ids=["F1"]))
        assert bridge.slack.posted == [], "inline style must not post a separate text message"
        upload = bridge.slack.uploads[0]
        assert upload["files"] == [("F1.pdf", b"mm-bytes")]
        assert upload["comment"].startswith("*mm-M1 (via Mattermost):*")
    finally:
        bridge.store.close()


def test_inline_style_still_records_the_link(config):
    bridge = _inline_bridge(config)
    try:
        bridge.handle_mattermost_event(mm_post("f", post_id="mm1", file_ids=["F1"]))
        ts = bridge.slack.uploads[0]["ts"]
        assert bridge.store.link_by_mattermost("M1", "mm1").slack_ts == ts
    finally:
        bridge.store.close()


def test_inline_style_leaves_fileless_messages_alone(config):
    bridge = _inline_bridge(config)
    try:
        bridge.handle_mattermost_event(mm_post("just text"))
        assert bridge.slack.posted[0]["username"] == "mm-M1 (via Mattermost)"
        assert bridge.slack.uploads == []
    finally:
        bridge.store.close()


def test_thread_style_keeps_the_author_on_the_message(bridge):
    assert bridge.config.slack_file_style == "thread"
    bridge.handle_mattermost_event(mm_post("here you go", file_ids=["F1"]))
    assert bridge.slack.posted[0]["username"] == "mm-M1 (via Mattermost)"
    assert bridge.slack.uploads[0]["thread_ts"] == bridge.slack.posted[0]["ts"]


# -- configured icons -----------------------------------------------------------

LOGO = "https://partner.example/logo.png"


def test_a_slack_author_with_their_own_picture_keeps_it(bridge):
    bridge.config.mattermost_icon_url = LOGO
    bridge.handle_slack_event(slack_event("hi"))
    assert bridge.mattermost.posted[0]["icon_url"] == "https://cdn/a.png"


def test_the_configured_icon_stands_in_for_a_generated_default(bridge):
    bridge.config.mattermost_icon_url = LOGO
    bridge.slack.custom_avatars = False
    bridge.handle_slack_event(slack_event("hi"))
    bridge.handle_slack_event(slack_event("with a file", ts="100.2",
                                          files=[{"id": "F1", "name": "r.pdf"}]))
    assert [p["icon_url"] for p in bridge.mattermost.posted] == [LOGO, LOGO]


def test_without_a_configured_icon_the_slack_default_is_kept(bridge):
    bridge.slack.custom_avatars = False
    bridge.handle_slack_event(slack_event("hi"))
    assert bridge.mattermost.posted[0]["icon_url"] == "https://cdn/a.png"


def test_the_mattermost_icon_does_not_leak_into_slack(bridge):
    bridge.config.mattermost_icon_url = LOGO
    bridge.handle_mattermost_event(mm_post("hi"))
    assert bridge.slack.posted[0]["icon_url"] == ""


# -- reactions with several reactors ---------------------------------------------

def _react(bridge, added, user="U2", emoji="eyes", ts="100.1"):
    bridge.handle_slack_event({"event_id": f"E{added}{user}{emoji}", "event": {
        "type": "reaction_added" if added else "reaction_removed", "user": user,
        "reaction": emoji, "item": {"type": "message", "channel": "C1", "ts": ts}}})


def test_one_of_two_slack_reactors_leaving_keeps_the_mirrored_reaction(bridge):
    """The bridge's single reaction stands in for all of them."""
    bridge.handle_slack_event(slack_event("look", ts="100.1"))
    _react(bridge, True, user="U2"); _react(bridge, True, user="U3")
    bridge.slack.holders[("100.1", "eyes")] = ["U3"]          # U2 has just removed theirs
    _react(bridge, False, user="U2")
    assert bridge.mattermost.reactions_removed == []


def test_the_last_slack_reactor_leaving_removes_it(bridge):
    bridge.handle_slack_event(slack_event("look", ts="100.1"))
    post_id = bridge.mattermost.posted[0]["id"]
    _react(bridge, True)
    bridge.slack.holders[("100.1", "eyes")] = []
    _react(bridge, False)
    assert bridge.mattermost.reactions_removed == [(post_id, "eyes")]


def test_the_bridges_own_reaction_does_not_count_as_someone_still_reacting(bridge):
    """It may be there mirroring a Mattermost reactor -- not a reason to keep this one."""
    bridge.handle_slack_event(slack_event("look", ts="100.1"))
    _react(bridge, True)
    bridge.slack.holders[("100.1", "eyes")] = [bridge.slack.bot_user_id]
    _react(bridge, False)
    assert len(bridge.mattermost.reactions_removed) == 1


def test_one_of_two_mattermost_reactors_leaving_keeps_the_slack_reaction(bridge):
    bridge.handle_mattermost_event(mm_post("look", post_id="mm1"))
    bridge.mattermost.holders[("mm1", "eyes")] = ["M3"]
    bridge.handle_mattermost_event({"event": "reaction_removed", "data": {"reaction": json.dumps(
        {"user_id": "M2", "post_id": "mm1", "emoji_name": "eyes"})}})
    assert bridge.slack.reactions_removed == []


def test_an_unreadable_reaction_list_falls_back_to_removing(bridge):
    bridge.handle_slack_event(slack_event("look", ts="100.1"))
    def broken(*args):
        raise RuntimeError("slack is down")
    bridge.slack.reactors = broken
    _react(bridge, True); _react(bridge, False)
    assert len(bridge.mattermost.reactions_removed) == 1


# -- replies shown in the Slack channel too ---------------------------------------

def test_a_mattermost_reply_is_also_shown_in_the_slack_channel(bridge):
    bridge.handle_mattermost_event(mm_post("root", post_id="mm1"))
    bridge.handle_mattermost_event(mm_post("reply", post_id="mm2", root_id="mm1"))
    reply = bridge.slack.posted[1]
    assert reply["thread_ts"] == bridge.slack.posted[0]["ts"]
    assert reply["broadcast"] is True


def test_a_root_message_is_not_broadcast(bridge):
    bridge.handle_mattermost_event(mm_post("root", post_id="mm1"))
    assert bridge.slack.posted[0]["broadcast"] is False


def test_a_reply_whose_root_never_crossed_becomes_a_plain_root(bridge):
    bridge.handle_mattermost_event(mm_post("orphan", post_id="mm2", root_id="never-bridged"))
    assert bridge.slack.posted[0]["thread_ts"] is None
    assert bridge.slack.posted[0]["broadcast"] is False


def test_broadcasting_can_be_switched_off(bridge):
    bridge.config.slack_broadcast_replies = False
    bridge.handle_mattermost_event(mm_post("root", post_id="mm1"))
    bridge.handle_mattermost_event(mm_post("reply", post_id="mm2", root_id="mm1"))
    assert bridge.slack.posted[1]["broadcast"] is False


def test_the_file_thread_under_a_root_message_is_not_broadcast(bridge):
    """That thread is a workaround for Slack's file limitation, not a conversation."""
    bridge.handle_mattermost_event(mm_post("see file", post_id="mm1", file_ids=["F1"]))
    assert bridge.slack.posted[0]["broadcast"] is False
    assert bridge.slack.uploads[0]["thread_ts"] == bridge.slack.posted[0]["ts"]


def test_a_broadcast_reply_does_not_echo_back(bridge):
    bridge.handle_mattermost_event(mm_post("root", post_id="mm1"))
    bridge.handle_mattermost_event(mm_post("reply", post_id="mm2", root_id="mm1"))
    reply = bridge.slack.posted[1]
    bridge.handle_slack_event(slack_event("reply", ts=reply["ts"], user="U2",
                                          subtype="thread_broadcast",
                                          thread_ts=bridge.slack.posted[0]["ts"]))
    assert len(bridge.mattermost.posted) == 0


# -- mentions across the bridge ------------------------------------------------------

def _slack_person(uid, username, display, real):
    from mmbridge.models import BridgeUser
    return BridgeUser(platform="slack", user_id=uid, display_name=display, username=username, real_name=real)


def _mm_person(uid, username, display, real):
    from mmbridge.models import BridgeUser
    return BridgeUser(platform="mattermost", user_id=uid, display_name=display, username=username, real_name=real)


def test_a_slack_colleague_can_be_mentioned_from_mattermost(bridge):
    bridge.slack.channel_people = [_slack_person("U-ANNA", "anna.mueller", "Anna", "Anna Müller")]
    bridge.handle_mattermost_event(mm_post("@Anna can you check?"))
    assert bridge.slack.posted[0]["text"] == "<@U-ANNA> can you check?"


def test_a_mattermost_username_is_left_to_mattermost(bridge):
    """"@femi" names a Mattermost person; it must not become a Slack Femi."""
    bridge.mattermost.usernames = {"femi": "M-FEMI"}
    bridge.slack.channel_people = [_slack_person("U-F", "femi", "Femi", "Femi Slack")]
    bridge.handle_mattermost_event(mm_post("@femi see this"))
    assert bridge.slack.posted[0]["text"] == "@femi see this"


def test_an_ambiguous_name_from_mattermost_stays_plain(bridge):
    bridge.slack.channel_people = [_slack_person("U1", "anna.m", "Anna", "Anna Müller"),
                                   _slack_person("U2", "anna.k", "Anna", "Anna Klein")]
    bridge.handle_mattermost_event(mm_post("@anna hi"))
    assert bridge.slack.posted[0]["text"] == "@anna hi"


def test_a_mattermost_colleague_can_be_mentioned_from_slack(bridge):
    bridge.mattermost.channel_people = [_mm_person("M1", "ada_l", "Ada", "Ada Lovelace")]
    bridge.handle_slack_event(slack_event("thanks @Ada!"))
    assert bridge.mattermost.posted[0]["message"] == "thanks @ada_l!"


def test_a_slack_mention_of_a_slack_only_person_cannot_ping_in_mattermost(bridge):
    from mmbridge.mdconv import NO_PING
    bridge.handle_slack_event(slack_event("ask <@U2>"))
    message = bridge.mattermost.posted[0]["message"]
    assert message == "ask @" + NO_PING + "slack-U2"


def test_an_edited_slack_message_resolves_mentions_too(bridge):
    bridge.mattermost.channel_people = [_mm_person("M1", "ada_l", "Ada", "Ada Lovelace")]
    bridge.handle_slack_event(slack_event("before", ts="100.1"))
    bridge.handle_slack_event({"event_id": "Ed", "event": {
        "type": "message", "subtype": "message_changed", "channel": "C1",
        "message": {"ts": "100.1", "text": "cc @Ada", "user": "U1"},
        "previous_message": {"ts": "100.1", "text": "before"}}})
    assert bridge.mattermost.updated[0]["message"] == "cc @ada_l"
