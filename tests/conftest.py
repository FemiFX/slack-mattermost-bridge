"""Fake adapters, so the routing rules can be tested without touching a network."""
from __future__ import annotations

import pytest

from mmbridge.config import ChannelPair, Config
from mmbridge.models import MATTERMOST, SLACK, BridgeFile, BridgeMessage, BridgeUser


class FakeSlack:
    bot_user_id = "UBOT"
    bot_id = "BBOT"

    def __init__(self):
        self.posted, self.updated, self.deleted = [], [], []
        self.reactions_added, self.reactions_removed, self.uploads = [], [], []
        self.next_ts = iter(f"200.{n}" for n in range(1, 500))
        # Channel membership, for the allowlist enforcement tests.
        self.members, self.left, self.notices = [], [], []
        self.missing_channels = set()   # channel IDs that do not resolve
        self.leave_result = "left"
        self.custom_avatars = True      # whether authors chose their own picture
        self.holders = {}               # (ts, emoji) -> user IDs still reacting
        self.channel_people = []        # BridgeUsers in the bridged channel

    def channel_members(self, channel):
        return list(self.channel_people)

    def reactors(self, channel, ts, emoji):
        return list(self.holders.get((ts, emoji), []))

    def member_channels(self):
        return list(self.members)

    def channel_exists(self, channel):
        return channel not in self.missing_channels

    def leave(self, channel):
        self.left.append(channel)
        return self.leave_result

    def post_as_app(self, channel, text):
        self.notices.append((channel, text))

    def user(self, user_id):
        return BridgeUser(platform=SLACK, user_id=user_id, display_name=f"slack-{user_id}",
                          username=f"slack-{user_id}", avatar_url="https://cdn/a.png",
                          email=f"{user_id}@example.org",
                          has_custom_avatar=self.custom_avatars)

    def to_bridge_message(self, event):
        return BridgeMessage(
            source=SLACK, channel_id=event["channel"], message_id=event["ts"],
            author=self.user(event.get("user", "U1")), text=event.get("text", ""),
            thread_root_id=event.get("thread_ts") if event.get("thread_ts") != event["ts"] else None,
            files=[BridgeFile(file_id=f["id"], name=f["name"], source_ref=f.get("url", "u"))
                   for f in event.get("files", [])],
        )

    def to_bridge_reaction(self, event):
        from mmbridge.models import BridgeReaction
        item = event.get("item", {})
        return BridgeReaction(source=SLACK, channel_id=item.get("channel", ""),
                              message_id=item.get("ts", ""), emoji=event.get("reaction", ""),
                              user=self.user(event.get("user", "U1")))

    def post(self, channel, text, username="", icon_url="", thread_ts=None, broadcast=False):
        ts = next(self.next_ts)
        self.posted.append({"channel": channel, "text": text, "username": username,
                            "icon_url": icon_url, "thread_ts": thread_ts,
                            "broadcast": broadcast, "ts": ts})
        return ts

    def update(self, channel, ts, text):
        self.updated.append({"channel": channel, "ts": ts, "text": text})

    def delete(self, channel, ts):
        self.deleted.append({"channel": channel, "ts": ts})

    def add_reaction(self, channel, ts, emoji):
        self.reactions_added.append((channel, ts, emoji))

    def remove_reaction(self, channel, ts, emoji):
        self.reactions_removed.append((channel, ts, emoji))

    def upload_files(self, channel, files, thread_ts=None, comment=""):
        ts = next(self.next_ts)
        self.uploads.append({"channel": channel, "files": files,
                             "thread_ts": thread_ts, "comment": comment, "ts": ts})
        return ts

    def download(self, bridge_file):
        return b"slack-bytes"

    def index_users_by_email(self):
        return {}

    def find_user_id_by_name(self, name):
        return None


class FakeMattermost:
    bot_user_id = "MMBOT"

    def __init__(self, is_bot_account=False):
        self.is_bot_account = is_bot_account
        self.posted, self.updated, self.deleted = [], [], []
        self.reactions_added, self.reactions_removed, self.uploaded = [], [], []
        self.holders = {}               # (post_id, emoji) -> user IDs still reacting
        self.channel_people = []        # BridgeUsers in the bridged channel
        self.usernames = {}             # Mattermost username -> user ID
        self.next_id = iter(f"post{n}" for n in range(1, 500))

    def user(self, user_id):
        return BridgeUser(platform=MATTERMOST, user_id=user_id, display_name=f"mm-{user_id}",
                          username=f"mm-{user_id}", email=f"{user_id}@example.org")

    def user_by_email(self, email):
        return None

    def find_user_id_by_name(self, name):
        return self.usernames.get(name)

    def channel_members(self, channel_id):
        return list(self.channel_people)

    def to_bridge_message(self, post):
        return BridgeMessage(
            source=MATTERMOST, channel_id=post["channel_id"], message_id=post["id"],
            author=self.user(post.get("user_id", "M1")), text=post.get("message", ""),
            thread_root_id=post.get("root_id") or None,
            files=[BridgeFile(file_id=f, name=f"{f}.pdf", source_ref=f)
                   for f in post.get("file_ids", [])],
        )

    def post(self, channel_id, message, username="", icon_url="", root_id=None, file_ids=None):
        post_id = next(self.next_id)
        self.posted.append({"channel_id": channel_id, "message": message, "username": username,
                            "icon_url": icon_url, "root_id": root_id, "file_ids": file_ids,
                            "id": post_id})
        return post_id

    def update(self, post_id, message):
        self.updated.append({"id": post_id, "message": message})

    def delete(self, post_id):
        self.deleted.append(post_id)

    def add_reaction(self, post_id, emoji):
        self.reactions_added.append((post_id, emoji))

    def remove_reaction(self, post_id, emoji):
        self.reactions_removed.append((post_id, emoji))

    def reactors(self, post_id, emoji):
        return list(self.holders.get((post_id, emoji), []))

    def upload_file(self, channel_id, filename, data):
        file_id = f"mmfile{len(self.uploaded) + 1}"
        self.uploaded.append({"channel_id": channel_id, "filename": filename, "data": data})
        return file_id

    def download(self, bridge_file):
        return b"mm-bytes"


@pytest.fixture
def config(tmp_path):
    return Config(
        slack_bot_token="xoxb-test", slack_app_token="xapp-test",
        mattermost_url="https://mm.test", mattermost_token="mmtoken",
        pairs=[ChannelPair("C1", "M1", "test")],
        db_path=str(tmp_path / "bridge.sqlite3"),
    )


@pytest.fixture
def bridge(config):
    from mmbridge.bridge import Bridge

    instance = Bridge(config, slack=FakeSlack(), mattermost=FakeMattermost())
    yield instance
    instance.store.close()


def slack_event(text="hello", ts="100.1", user="U1", channel="C1", **extra):
    event = {"type": "message", "channel": channel, "ts": ts, "user": user, "text": text}
    event.update(extra)
    return {"event_id": f"Ev{ts}", "event": event}


def mm_post(message="hello", post_id="mm1", user_id="M1", channel_id="M1", **extra):
    post = {"id": post_id, "channel_id": channel_id, "user_id": user_id,
            "message": message, "type": ""}
    post.update(extra)
    return {"event": "posted", "data": {"post": __import__("json").dumps(post)}}
