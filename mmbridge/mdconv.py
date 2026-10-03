"""Translation between Slack mrkdwn and Mattermost markdown.

The two look similar enough to be mistaken for the same format, which is the
trap. Slack uses ``*bold*``, ``~strike~`` and ``<url|label>``; Mattermost uses
standard markdown's ``**bold**``, ``~~strike~~`` and ``[label](url)``. Passing
text through unconverted silently mangles emphasis and turns every link into
visible angle-bracket noise.

Code spans and fenced blocks are held out of the conversion entirely -- a
``*`` inside a snippet is a character, not emphasis.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

# Sentinels stand in for a finished replacement so a later rule cannot rewrite
# it again -- e.g. so ``**bold**`` reduced to ``*bold*`` is not then read as
# italic. They are control characters that cannot occur in chat text.
_BOLD = "\x00B\x00"
_ITALIC = "\x00I\x00"
_STRIKE = "\x00S\x00"

_CODE_RE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)

UserResolver = Callable[[str], Optional[str]]
MentionResolver = Callable[[str], Optional[str]]

# Placed between "@" and a name that must not become a mention. It is invisible,
# but it stops Mattermost reading "@Anna" as the username "anna" -- which would
# notify whoever holds that username instead of the Slack person who was meant.
NO_PING = "\u2060"

# "@name" in running text. Not after a word character, slash, colon, dot or @,
# so e-mail addresses and URLs such as example.org/@anna are left alone. Names
# may contain any letters, so "@Müller" is read whole.
_MENTION_RE = re.compile(r"(?<![\w/:.@])@(\w[\w.\-]*)")


def unpinged(name: str) -> str:
    """Render a name after "@" without it becoming a mention."""
    return f"@{NO_PING}{name}"


def _split_trailing(name: str) -> Tuple[str, str]:
    """Separate sentence punctuation: "anna." is the name "anna" plus "."."""
    stripped = name.rstrip("._-")
    return stripped, name[len(stripped):]


def _split_code(text: str) -> List[Tuple[bool, str]]:
    """Split into (is_code, segment) runs, preserving order."""
    return [(bool(i % 2), part) for i, part in enumerate(_CODE_RE.split(text))]


def _outside_code(text: str, transform: Callable[[str], str]) -> str:
    return "".join(part if is_code else transform(part) for is_code, part in _split_code(text))


# --- Slack -> Mattermost ----------------------------------------------------

_SLACK_LINK_RE = re.compile(r"<(?P<target>[^<>|]+)(?:\|(?P<label>[^<>]*))?>")


def _slack_entity(match: "re.Match[str]", resolve_user: Optional[UserResolver]) -> str:
    target = match.group("target")
    label = match.group("label")

    if target.startswith("@"):
        # The resolver returns the text to insert: a real Mattermost mention
        # when the person has an account there, an unpinged name otherwise.
        user_id = target[1:]
        rendered = resolve_user(user_id) if resolve_user else None
        return rendered or unpinged(label or user_id)
    if target.startswith("#"):
        # <#C0123|general> -- Mattermost writes channel links as ~name.
        channel_name = label or target[1:]
        return f"~{channel_name}"
    if target.startswith("!"):
        special = target[1:]
        if special.startswith("subteam^"):
            # A Slack user group has no Mattermost counterpart.
            return unpinged(label.lstrip("@") if label else "group")
        if special in {"here", "channel", "everyone"}:
            return f"@{special}"
        return label or f"@{special}"
    # An ordinary URL: <https://x|label> or <https://x>.
    if label and label != target:
        return f"[{label}]({target})"
    return target


def slack_to_mattermost(
    text: str,
    resolve_user: Optional[UserResolver] = None,
    resolve_plain: Optional[MentionResolver] = None,
) -> str:
    """Convert Slack mrkdwn to Mattermost markdown.

    `resolve_user` renders a real Slack mention (<@U…>). `resolve_plain` maps a
    plain "@name" -- typed for someone Slack does not know, i.e. a Mattermost
    person -- to a Mattermost username, or None to leave it as typed.
    """
    if not text:
        return ""

    def convert(segment: str) -> str:
        # Slack escapes these three on the way out; undo that before anything
        # else so the rest of the rules see the characters the user typed.
        segment = segment.replace("&lt;", "\x00LT\x00").replace("&gt;", "\x00GT\x00")
        segment = _SLACK_LINK_RE.sub(lambda m: _slack_entity(m, resolve_user), segment)
        segment = segment.replace("\x00LT\x00", "<").replace("\x00GT\x00", ">")
        segment = segment.replace("&amp;", "&")

        if resolve_plain:
            def plain(match: "re.Match[str]") -> str:
                name, trailing = _split_trailing(match.group(1))
                if not name or name.casefold() in {"here", "channel", "all", "everyone"}:
                    return match.group(0)
                username = resolve_plain(name)
                return f"@{username}{trailing}" if username else match.group(0)

            segment = _MENTION_RE.sub(plain, segment)

        segment = re.sub(r"(?<![~\w])~(?!\s)([^~\n]+?)(?<!\s)~(?![~\w])", _STRIKE + r"\1" + _STRIKE, segment)
        segment = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", _BOLD + r"\1" + _BOLD, segment)

        return segment.replace(_STRIKE, "~~").replace(_BOLD, "**")

    return _outside_code(text, convert)


# --- Mattermost -> Slack ----------------------------------------------------

_MD_LINK_RE = re.compile(r"\[(?P<label>[^\]\n]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")


def mattermost_to_slack(text: str, resolve_mention: Optional[MentionResolver] = None) -> str:
    if not text:
        return ""

    def convert(segment: str) -> str:
        # Escape first: Slack requires it, and doing it before links are built
        # keeps the angle brackets we are about to add from being escaped too.
        segment = segment.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        segment = _MD_LINK_RE.sub(
            lambda m: "<{}|{}>".format(m.group("target"), m.group("label") or m.group("target")),
            segment,
        )

        segment = re.sub(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", _BOLD + r"\1" + _BOLD, segment)
        segment = re.sub(r"__(?!\s)(.+?)(?<!\s)__", _BOLD + r"\1" + _BOLD, segment)
        segment = re.sub(r"~~(?!\s)(.+?)(?<!\s)~~", _STRIKE + r"\1" + _STRIKE, segment)
        # Whatever single asterisks remain were italics in markdown; Slack
        # spells italics with underscores.
        segment = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", _ITALIC + r"\1" + _ITALIC, segment)

        if resolve_mention:
            def mention(match: "re.Match[str]") -> str:
                name, trailing = _split_trailing(match.group(1))
                lowered = name.casefold()
                if lowered in {"here", "channel", "all"}:
                    return f"<!{'channel' if lowered == 'all' else lowered}>{trailing}"
                slack_id = resolve_mention(name) if name else None
                return f"<@{slack_id}>{trailing}" if slack_id else match.group(0)

            segment = _MENTION_RE.sub(mention, segment)

        return (
            segment.replace(_BOLD, "*").replace(_STRIKE, "~").replace(_ITALIC, "_")
        )

    return _outside_code(text, convert)
