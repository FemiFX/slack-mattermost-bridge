import re

from mmbridge.mdconv import NO_PING, mattermost_to_slack, slack_to_mattermost


def test_slack_emphasis_becomes_markdown():
    assert slack_to_mattermost("*bold* _it_ ~gone~") == "**bold** _it_ ~~gone~~"


def test_slack_links_become_markdown():
    assert slack_to_mattermost("<https://x.dev|docs>") == "[docs](https://x.dev)"
    assert slack_to_mattermost("<https://x.dev>") == "https://x.dev"


def test_slack_mentions_resolve_through_the_callback():
    text = "hi <@U1> in <#C1|general> <!here>"
    assert slack_to_mattermost(text, lambda uid: "@anna") == "hi @anna in ~general @here"


def test_an_unresolvable_slack_mention_cannot_ping_anyone_in_mattermost():
    """Rendered as "@U1" it would notify a Mattermost user who happens to be u1."""
    out = slack_to_mattermost("hi <@U1>", lambda uid: None)
    assert out == "hi @" + NO_PING + "U1"
    assert not re.search(r"@\w", out)


def test_slack_entities_are_unescaped():
    assert slack_to_mattermost("5 &lt; 6 &amp; 7 &gt; 2") == "5 < 6 & 7 > 2"


def test_code_is_left_alone_in_both_directions():
    assert slack_to_mattermost("`*keep*` and *go*") == "`*keep*` and **go**"
    assert mattermost_to_slack("`**keep**` and **go**") == "`**keep**` and *go*"
    assert slack_to_mattermost("```a*b*c```") == "```a*b*c```"


def test_markdown_emphasis_becomes_slack():
    assert mattermost_to_slack("**b** *i* ~~s~~ __b2__") == "*b* _i_ ~s~ *b2*"


def test_markdown_links_become_slack():
    assert mattermost_to_slack("[docs](https://x.dev)") == "<https://x.dev|docs>"


def test_slack_special_characters_are_escaped():
    assert mattermost_to_slack("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_mattermost_mentions_resolve_to_slack_ids():
    resolve = lambda name: "U9" if name == "anna" else None
    assert mattermost_to_slack("@anna and @bob", resolve) == "<@U9> and @bob"
    assert mattermost_to_slack("@here", resolve) == "<!here>"


def test_a_round_trip_preserves_meaning():
    # Not byte-identical -- Slack has one italic spelling and markdown has two --
    # but nothing may be lost or doubled on the way back.
    original = "**bold** and [docs](https://x.dev) and ~~gone~~"
    assert slack_to_mattermost(mattermost_to_slack(original)) == original


def test_empty_text_is_safe():
    assert slack_to_mattermost("") == ""
    assert mattermost_to_slack("") == ""


# -- mentions ---------------------------------------------------------------------

def test_a_slack_user_group_cannot_ping_a_mattermost_namesake():
    out = slack_to_mattermost("<!subteam^S1|@design>")
    assert out == "@" + NO_PING + "design"


def test_a_plain_name_typed_in_slack_becomes_a_mattermost_mention():
    resolve = lambda name: "ada_l" if name == "Ada" else None
    assert slack_to_mattermost("thanks @Ada.", resolve_plain=resolve) == "thanks @ada_l."


def test_an_unmatched_plain_name_is_left_as_typed():
    assert slack_to_mattermost("hi @nobody", resolve_plain=lambda n: None) == "hi @nobody"


def test_a_mattermost_mention_keeps_its_trailing_punctuation():
    assert mattermost_to_slack("thanks @anna, and @ben.", lambda n: {"anna": "UA", "ben": "UB"}.get(n)) \
        == "thanks <@UA>, and <@UB>."


def test_names_with_umlauts_are_read_whole():
    seen = []
    mattermost_to_slack("hallo @Müller", lambda n: seen.append(n))
    assert seen == ["Müller"]


def test_email_addresses_and_urls_are_not_mentions_in_either_direction():
    anything = lambda name: "UX"
    assert mattermost_to_slack("mail anna@example.org or see x.org/@anna", anything) \
        == "mail anna@example.org or see x.org/@anna"
    assert slack_to_mattermost("mail anna@example.org", resolve_plain=lambda n: "someone") \
        == "mail anna@example.org"


def test_channel_wide_mentions_are_case_insensitive():
    assert mattermost_to_slack("@ALL listen", lambda n: None) == "<!channel> listen"
