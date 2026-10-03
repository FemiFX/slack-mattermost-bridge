"""The platform-neutral message model.

Adapters translate into and out of these types; the bridge core never sees a
Slack or Mattermost payload. Adding a third platform later means writing one
adapter, not touching the routing logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SLACK = "slack"
MATTERMOST = "mattermost"


def other_platform(platform: str) -> str:
    return MATTERMOST if platform == SLACK else SLACK


@dataclass
class BridgeUser:
    platform: str
    user_id: str
    display_name: str
    username: str = ""
    avatar_url: str = ""
    email: str = ""
    is_bot: bool = False
    # Full name as the platform holds it, for matching typed @names.
    real_name: str = ""
    # Whether avatar_url is a picture the person chose, rather than a generated
    # default. Only Slack users are classified; see slack_adapter.has_own_avatar.
    has_custom_avatar: bool = False


@dataclass
class BridgeFile:
    """A file on the source platform, not yet downloaded.

    ``source_ref`` is whatever the source adapter needs to fetch the bytes: a
    Slack ``url_private`` or a Mattermost file ID. Downloads happen lazily, so a
    message that gets dropped as a loop never costs a file transfer.
    """

    file_id: str
    name: str
    size: int = 0
    mimetype: str = ""
    source_ref: str = ""


@dataclass
class BridgeMessage:
    source: str
    channel_id: str
    message_id: str
    author: BridgeUser
    text: str = ""
    thread_root_id: Optional[str] = None
    files: List[BridgeFile] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_thread_reply(self) -> bool:
        return bool(self.thread_root_id) and self.thread_root_id != self.message_id


@dataclass
class BridgeReaction:
    source: str
    channel_id: str
    message_id: str
    emoji: str
    user: BridgeUser


@dataclass
class MessageLink:
    """The identity of one logical message across both platforms."""

    slack_channel: str
    slack_ts: str
    mattermost_channel: str
    mattermost_post_id: str
    origin: str
    slack_thread_ts: Optional[str] = None
    mattermost_root_id: Optional[str] = None

    def counterpart(self, platform: str) -> str:
        """The message ID on `platform`'s side of this link."""
        return self.slack_ts if platform == SLACK else self.mattermost_post_id
