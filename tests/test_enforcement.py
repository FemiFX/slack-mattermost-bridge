"""Slack channel allowlist enforcement: leave anywhere not in MMBRIDGE_PAIRS."""
from conftest import FakeMattermost, FakeSlack, slack_event
from mmbridge.bridge import Bridge


def joined(channel, user="UBOT"):
    return {"event_id": f"Ej{channel}{user}", "event": {
        "type": "member_joined_channel", "user": user, "channel": channel,
        "channel_type": "C", "team": "T1", "inviter": "U9",
    }}


def test_being_invited_to_an_unauthorised_channel_posts_the_notice_and_leaves(bridge):
    bridge.handle_slack_event(joined("C-ROGUE"))
    assert bridge.slack.notices[0][0] == "C-ROGUE"
    assert "nicht weitergeleitet" in bridge.slack.notices[0][1]
    assert bridge.slack.left == ["C-ROGUE"]


def test_being_invited_to_the_allowlisted_channel_is_left_alone(bridge):
    bridge.handle_slack_event(joined("C1"))
    assert bridge.slack.left == []
    assert bridge.slack.notices == []


def test_someone_else_joining_is_not_our_business(bridge):
    bridge.handle_slack_event(joined("C-ROGUE", user="U-SOMEONE"))
    assert bridge.slack.left == []


def test_a_message_from_an_unauthorised_channel_triggers_leaving_and_is_not_mirrored(bridge):
    """A message proves membership, even if the join event never arrived."""
    bridge.handle_slack_event(slack_event("secret", channel="C-ROGUE"))
    assert bridge.slack.left == ["C-ROGUE"]
    assert bridge.mattermost.posted == []


def test_the_notice_is_posted_only_once_when_leaving_is_not_permitted(bridge):
    """Without channels:manage the app stays; it must not repeat itself per message."""
    bridge.slack.leave_result = "missing_scope"
    for n in range(3):
        bridge.handle_slack_event(slack_event(f"m{n}", ts=f"300.{n}", channel="C-ROGUE"))
    assert len(bridge.slack.notices) == 1
    assert bridge.mattermost.posted == []


def test_a_fresh_invitation_announces_again(bridge):
    bridge.handle_slack_event(joined("C-ROGUE"))
    bridge.handle_slack_event({**joined("C-ROGUE"), "event_id": "again"})
    assert len(bridge.slack.notices) == 2


def test_an_empty_notice_leaves_silently(bridge):
    bridge.config.slack_leave_notice = ""
    bridge.handle_slack_event(joined("C-ROGUE"))
    assert bridge.slack.notices == []
    assert bridge.slack.left == ["C-ROGUE"]


def test_with_leaving_switched_off_the_channel_is_only_ignored(bridge):
    bridge.config.slack_leave_unauthorised = False
    bridge.handle_slack_event(slack_event("x", channel="C-ROGUE"))
    assert bridge.slack.left == []
    assert bridge.slack.notices == []
    assert bridge.mattermost.posted == []


def test_direct_messages_are_never_left(bridge):
    bridge.handle_slack_event(slack_event("hi", channel="D0123"))
    assert bridge.slack.left == []


def test_enforcement_holds_for_a_one_way_mm_to_slack_bridge(bridge):
    """It is a security control, not a mirroring feature."""
    bridge.config.direction = "mm_to_slack"
    bridge.handle_slack_event(joined("C-ROGUE"))
    assert bridge.slack.left == ["C-ROGUE"]


# -- the sweep ----------------------------------------------------------------

def test_the_sweep_leaves_every_channel_not_on_the_allowlist(bridge):
    bridge.slack.members = [{"id": "C1", "name": "bridged"},
                            {"id": "C-ROGUE", "name": "random"},
                            {"id": "G-PRIVATE", "name": "hr"}]
    bridge.reconcile_slack_channels()
    assert sorted(bridge.slack.left) == ["C-ROGUE", "G-PRIVATE"]


def test_the_sweep_refuses_to_act_if_a_configured_channel_cannot_be_found(bridge):
    """A typo in MMBRIDGE_PAIRS must not make the bridge walk out of its real channel."""
    bridge.slack.missing_channels = {"C1"}
    bridge.slack.members = [{"id": "C-REAL", "name": "the-actual-channel"}]
    bridge.reconcile_slack_channels()
    assert bridge.slack.left == []


def test_the_sweep_does_nothing_when_leaving_is_off(bridge):
    bridge.config.slack_leave_unauthorised = False
    bridge.slack.members = [{"id": "C-ROGUE", "name": "random"}]
    bridge.reconcile_slack_channels()
    assert bridge.slack.left == []


def test_the_sweep_survives_a_slack_failure(config):
    class Broken(FakeSlack):
        def member_channels(self):
            raise RuntimeError("slack is down")

    bridge = Bridge(config, slack=Broken(), mattermost=FakeMattermost())
    try:
        bridge.reconcile_slack_channels()   # must log, not raise
    finally:
        bridge.store.close()
