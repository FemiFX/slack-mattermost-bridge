"""Slack side of the bridge: Socket Mode for events, Web API for everything else.

Socket Mode means the bridge dials out to Slack over a WebSocket rather than
Slack calling in, so the service can sit beside a self-hosted Mattermost with no
inbound firewall rule, no public hostname and no TLS certificate to renew.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from .models import SLACK, BridgeFile, BridgeMessage, BridgeReaction, BridgeUser

log = logging.getLogger(__name__)

# Subtypes that still represent somebody saying something. Everything else
# ("channel_join", "channel_topic", pinned-item notices and friends) is Slack
# talking about the channel rather than a message worth mirroring.
MESSAGE_SUBTYPES = {None, "", "file_share", "thread_broadcast", "me_message", "bot_message"}

# Profiles are re-read after this long, so a changed name or picture shows up
# without restarting the bridge.
USER_CACHE_TTL_S = 3600


def gravatar_probe_url(avatar_url: str) -> Optional[str]:
    """The same Gravatar image, but answering 404 instead of a default picture.

    Slack gives people without an uploaded picture a Gravatar address with its
    own default image as the fallback, so the address alone cannot say whether a
    real Gravatar exists. Asking for d=404 can.
    """
    parsed = urlparse(avatar_url)
    if not parsed.netloc.endswith("gravatar.com"):
        return None
    query = parse_qs(parsed.query)
    query["d"] = ["404"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def gravatar_exists(avatar_url: str) -> bool:
    probe = gravatar_probe_url(avatar_url)
    if not probe:
        return False
    try:
        return requests.head(probe, timeout=5, allow_redirects=True).status_code == 200
    except requests.RequestException:
        # Unknown counts as absent: the configured icon is a safer thing to show
        # than a Gravatar address Mattermost's readers may not be able to load.
        return False


def has_own_avatar(profile: dict, probe: Callable[[str], bool] = gravatar_exists) -> bool:
    """Whether a Slack profile carries a picture the person chose.

    True for a picture uploaded to Slack, or a real Gravatar; False for Slack's
    generated defaults.
    """
    if profile.get("is_custom_image"):
        return True
    avatar = profile.get("image_192") or profile.get("image_72") or ""
    return bool(avatar) and probe(avatar)


class SlackAdapter:
    def __init__(self, config, on_event: Optional[Callable] = None, token: str = "") -> None:
        self.config = config
        # With rotation the token comes from SlackTokenManager and is swapped on
        # this client in place (self.client.token) at every refresh, so nothing
        # else may hold on to a copy of it.
        self.client = WebClient(token=token or config.slack_bot_token)
        self.on_event = on_event

        self._users: Dict[str, Tuple[BridgeUser, float]] = {}
        self._users_lock = threading.Lock()
        self._socket: Optional[SocketModeClient] = None

        auth = self.client.auth_test()
        self.bot_user_id: str = auth["user_id"]
        self.team_id: str = auth.get("team_id", "")
        # bot_id identifies the *app* posting; message events carry it whenever
        # an app posted, which is the first and cheapest loop guard.
        self.bot_id: str = auth.get("bot_id", "")
        log.info("Slack authenticated as %s (bot_user=%s team=%s)",
                 auth.get("user"), self.bot_user_id, self.team_id)

    # -- identity ------------------------------------------------------------

    def user(self, user_id: str) -> BridgeUser:
        if not user_id:
            return BridgeUser(platform=SLACK, user_id="", display_name="Unknown")
        with self._users_lock:
            cached = self._users.get(user_id)
        if cached and time.monotonic() - cached[1] < USER_CACHE_TTL_S:
            return cached[0]

        try:
            profile = self.client.users_info(user=user_id)["user"]
        except SlackApiError as error:
            log.warning("Could not resolve Slack user %s: %s", user_id, error)
            return BridgeUser(platform=SLACK, user_id=user_id, display_name=user_id)

        details = profile.get("profile", {})
        display = (
            details.get("display_name")
            or details.get("real_name")
            or profile.get("real_name")
            or profile.get("name")
            or user_id
        )
        user = BridgeUser(
            platform=SLACK,
            user_id=user_id,
            display_name=display,
            username=profile.get("name", ""),
            avatar_url=details.get("image_192") or details.get("image_72") or "",
            email=details.get("email", ""),
            is_bot=bool(profile.get("is_bot")),
            real_name=details.get("real_name") or profile.get("real_name") or "",
            has_custom_avatar=has_own_avatar(details),
        )
        with self._users_lock:
            self._users[user_id] = (user, time.monotonic())
        return user

    def user_name(self, user_id: str) -> Optional[str]:
        """Display name for mention rendering; None when unresolvable."""
        user = self.user(user_id)
        return user.display_name or None

    def find_user_id_by_name(self, name: str) -> Optional[str]:
        """Best-effort @name -> Slack user ID, using whatever is already cached.

        Deliberately does not page the whole member list: a mention that cannot
        be resolved is rendered as plain text, which is a far better failure
        than stalling every message on a workspace-wide lookup.
        """
        needle = name.lower().lstrip("@")
        with self._users_lock:
            for user, _ in self._users.values():
                if needle in {user.username.lower(), user.display_name.lower()}:
                    return user.user_id
        return None

    def channel_members(self, channel: str) -> List[BridgeUser]:
        """The people in a channel -- no bots, and not this app itself."""
        members: List[BridgeUser] = []
        cursor = None
        while True:
            page = self.client.conversations_members(channel=channel, limit=200, cursor=cursor)
            for user_id in page["members"]:
                if user_id == self.bot_user_id:
                    continue
                user = self.user(user_id)
                if not user.is_bot:
                    members.append(user)
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return members

    def index_users_by_email(self) -> Dict[str, BridgeUser]:
        index: Dict[str, BridgeUser] = {}
        cursor = None
        while True:
            page = self.client.users_list(limit=200, cursor=cursor)
            for member in page["members"]:
                if member.get("deleted") or member.get("is_bot"):
                    continue
                email = member.get("profile", {}).get("email", "")
                user = self.user(member["id"])
                if email:
                    index[email.lower()] = user
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return index

    # -- inbound -------------------------------------------------------------

    def to_bridge_message(self, event: dict) -> Optional[BridgeMessage]:
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        if not channel or not ts:
            return None

        user_id = event.get("user") or event.get("bot_id") or ""
        author = self.user(user_id) if event.get("user") else BridgeUser(
            platform=SLACK,
            user_id=user_id,
            display_name=event.get("username") or "Slack app",
            is_bot=True,
        )

        thread_ts = event.get("thread_ts")
        files = [
            BridgeFile(
                file_id=item.get("id", ""),
                name=item.get("name") or item.get("title") or "file",
                size=int(item.get("size") or 0),
                mimetype=item.get("mimetype", ""),
                source_ref=item.get("url_private_download") or item.get("url_private", ""),
            )
            for item in event.get("files", [])
            if not item.get("is_external")
        ]

        return BridgeMessage(
            source=SLACK,
            channel_id=channel,
            message_id=ts,
            author=author,
            text=event.get("text", "") or "",
            thread_root_id=thread_ts if thread_ts and thread_ts != ts else None,
            files=files,
            raw=event,
        )

    def to_bridge_reaction(self, event: dict) -> Optional[BridgeReaction]:
        item = event.get("item", {})
        if item.get("type") != "message":
            return None
        return BridgeReaction(
            source=SLACK,
            channel_id=item.get("channel", ""),
            message_id=item.get("ts", ""),
            emoji=event.get("reaction", ""),
            user=self.user(event.get("user", "")),
        )

    # -- outbound ------------------------------------------------------------

    def post(
        self,
        channel: str,
        text: str,
        username: str = "",
        icon_url: str = "",
        thread_ts: Optional[str] = None,
        broadcast: bool = False,
    ) -> str:
        kwargs = {
            "channel": channel,
            "text": text or " ",
            # Suppress previews of links that came from the other platform;
            # the author already chose how to present them there.
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
            if broadcast:
                # Slack's "Also send to #channel": one message, shown both in
                # the thread and in the channel. Editing or deleting it acts on
                # both places at once.
                kwargs["reply_broadcast"] = True
        if username:
            kwargs["username"] = username
        if icon_url:
            kwargs["icon_url"] = icon_url

        try:
            response = self.client.chat_postMessage(**kwargs)
        except SlackApiError as error:
            if error.response.get("error") == "missing_scope" and username:
                # chat:write.customize is what allows a per-message username and
                # icon. Without it the post still belongs in the channel, so
                # fall back to the app identity rather than dropping a message.
                log.warning(
                    "Slack rejected the identity override (missing chat:write.customize). "
                    "Posting as the app instead; add the scope and reinstall to show "
                    "each author's own name."
                )
                kwargs.pop("username", None)
                kwargs.pop("icon_url", None)
                response = self.client.chat_postMessage(**kwargs)
            else:
                raise
        return response["ts"]

    def update(self, channel: str, ts: str, text: str) -> None:
        self.client.chat_update(channel=channel, ts=ts, text=text or " ")

    def delete(self, channel: str, ts: str) -> None:
        try:
            self.client.chat_delete(channel=channel, ts=ts)
        except SlackApiError as error:
            if error.response.get("error") != "message_not_found":
                raise

    def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        try:
            self.client.reactions_add(channel=channel, timestamp=ts, name=emoji)
        except SlackApiError as error:
            # already_reacted is benign; invalid_name means the emoji exists on
            # the other platform but not here, which is not worth failing over.
            if error.response.get("error") not in {"already_reacted", "invalid_name", "no_reaction"}:
                raise

    def reactors(self, channel: str, ts: str, emoji: str) -> List[str]:
        """User IDs currently holding `emoji` on a message, skin tones folded in."""
        message = self.client.reactions_get(channel=channel, timestamp=ts, full=True)["message"]
        users: List[str] = []
        for reaction in message.get("reactions", []):
            if reaction.get("name", "").split("::")[0] == emoji:
                users.extend(reaction.get("users", []))
        return users

    def remove_reaction(self, channel: str, ts: str, emoji: str) -> None:
        try:
            self.client.reactions_remove(channel=channel, timestamp=ts, name=emoji)
        except SlackApiError as error:
            if error.response.get("error") not in {"no_reaction", "invalid_name"}:
                raise

    def upload_files(
        self,
        channel: str,
        files: List[tuple],
        thread_ts: Optional[str] = None,
        comment: str = "",
    ) -> Optional[str]:
        """Upload (filename, bytes) pairs into `channel`, returning the message ts.

        Slack's ``files.upload`` was retired in 2024; ``files_upload_v2`` wraps
        the newer three-step external-upload flow. It posts as the app rather
        than the bridged author, so files are attached under the author's text
        message instead of standing alone in the channel.
        """
        uploads = [{"filename": name, "content": data} for name, data in files]
        kwargs = {"channel": channel, "file_uploads": uploads}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if comment:
            kwargs["initial_comment"] = comment
        response = self.client.files_upload_v2(**kwargs)
        return self._share_ts(response, channel)

    def _share_ts(self, response, channel: str) -> Optional[str]:
        """The timestamp of the message an upload created, if it can be found.

        The upload endpoints do not return the enclosing message directly; the
        timestamp shows up under the file's ``shares`` once Slack has posted it.
        Returning None is survivable -- the file is delivered either way, it just
        cannot be linked for later edits or deletes.
        """
        uploaded = response.get("files") or ([response["file"]] if response.get("file") else [])
        for item in uploaded:
            for visibility in ("public", "private"):
                for share in (item.get("shares", {}).get(visibility, {}) or {}).get(channel, []):
                    if share.get("ts"):
                        return share["ts"]
        for item in uploaded:
            if not item.get("id"):
                continue
            try:
                info = self.client.files_info(file=item["id"])["file"]
            except SlackApiError:
                continue
            for visibility in ("public", "private"):
                for share in (info.get("shares", {}).get(visibility, {}) or {}).get(channel, []):
                    if share.get("ts"):
                        return share["ts"]
        return None

    def download(self, bridge_file: BridgeFile) -> Optional[bytes]:
        if not bridge_file.source_ref:
            return None
        if bridge_file.size and bridge_file.size > self.config.max_file_bytes:
            log.warning(
                "Skipping Slack file %s (%.1f MB exceeds MMBRIDGE_MAX_FILE_MB=%d)",
                bridge_file.name, bridge_file.size / 1e6, self.config.max_file_mb,
            )
            return None
        response = requests.get(
            bridge_file.source_ref,
            headers={"Authorization": f"Bearer {self.client.token}"},
            timeout=120,
        )
        response.raise_for_status()
        return response.content

    # -- channel membership --------------------------------------------------

    def member_channels(self) -> List[dict]:
        """Every public and private channel the app is currently a member of."""
        return self.member_channels_with(self.client)

    @staticmethod
    def member_channels_with(client: WebClient) -> List[dict]:
        channels: List[dict] = []
        cursor = None
        while True:
            page = client.users_conversations(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            channels.extend(page["channels"])
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return channels

    def channel_exists(self, channel: str) -> bool:
        """Whether `channel` resolves in this workspace. Other errors propagate."""
        try:
            self.client.conversations_info(channel=channel)
            return True
        except SlackApiError as error:
            if error.response.get("error") == "channel_not_found":
                return False
            raise

    def leave(self, channel: str) -> str:
        """Leave `channel`. Returns "left", or Slack's error code on failure.

        Leaving needs channels:manage (public) or groups:write (private); a
        missing scope comes back as "missing_scope" rather than an exception so
        the caller can degrade instead of crash.
        """
        try:
            self.client.conversations_leave(channel=channel)
            return "left"
        except SlackApiError as error:
            code = error.response.get("error", "unknown_error")
            return "left" if code == "not_in_channel" else code

    def post_as_app(self, channel: str, text: str) -> None:
        """Post under the app's own identity -- for the bridge speaking for itself."""
        self.client.chat_postMessage(
            channel=channel, text=text, unfurl_links=False, unfurl_media=False,
        )

    # -- connection ----------------------------------------------------------

    def start(self) -> None:
        self._socket = SocketModeClient(
            app_token=self.config.slack_app_token,
            web_client=self.client,
        )

        def handle(client: SocketModeClient, request: SocketModeRequest) -> None:
            # Acknowledge immediately and unconditionally: Slack redelivers
            # anything unacknowledged within three seconds, and the store's
            # de-duplication is what makes a redelivery harmless.
            client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            if request.type != "events_api" or not self.on_event:
                return
            try:
                self.on_event(request.payload)
            except Exception:
                log.exception("Unhandled error while processing a Slack event")

        self._socket.socket_mode_request_listeners.append(handle)
        self._socket.connect()
        log.info("Slack Socket Mode connected")

    def stop(self) -> None:
        if self._socket:
            self._socket.close()
