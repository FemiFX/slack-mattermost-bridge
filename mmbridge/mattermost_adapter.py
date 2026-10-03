"""Mattermost side of the bridge: WebSocket for events, REST v4 for everything else.

Outgoing webhooks are deliberately not used. They only fire for public channels,
which rules out exactly the private partner channel this bridge exists to serve,
and they cannot carry file uploads.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests
import websocket

from .models import MATTERMOST, BridgeFile, BridgeMessage, BridgeUser

log = logging.getLogger(__name__)


class MattermostError(RuntimeError):
    pass


class MattermostAdapter:
    def __init__(self, config, on_event: Optional[Callable] = None) -> None:
        self.config = config
        self.base_url = config.mattermost_url.rstrip("/")
        self.api = f"{self.base_url}/api/v4"
        self.on_event = on_event

        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {config.mattermost_token}"})

        self._users: Dict[str, BridgeUser] = {}
        self._users_lock = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._stopping = threading.Event()

        me = self._request("GET", "/users/me")
        self.bot_user_id: str = me["id"]
        # Mattermost stamps from_bot="true" on every post by a Bot Account and
        # will not let it be unset. The webapp then renders the bot's own
        # identity and ignores override_username entirely, so a bot account can
        # never post under the original author's name. A plain user account with
        # an access token can. See identity_override handling in bridge.py.
        self.is_bot_account: bool = bool(me.get("is_bot"))
        log.info("Mattermost authenticated as %s (%s)%s", me.get("username"),
                 self.bot_user_id, " [bot account]" if self.is_bot_account else "")

    # -- HTTP ----------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.api}{path}"
        for attempt in range(4):
            response = self.session.request(method, url, timeout=60, **kwargs)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", 2 ** attempt))
                log.warning("Mattermost rate-limited %s %s; retrying in %.1fs", method, path, wait)
                time.sleep(wait)
                continue
            if response.status_code >= 400:
                raise MattermostError(
                    f"Mattermost {method} {path} returned HTTP {response.status_code}: "
                    f"{response.text[:400]}"
                )
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.content
        raise MattermostError(f"Mattermost {method} {path} kept returning 429 after 4 attempts.")

    # -- identity ------------------------------------------------------------

    def user(self, user_id: str) -> BridgeUser:
        if not user_id:
            return BridgeUser(platform=MATTERMOST, user_id="", display_name="Unknown")
        with self._users_lock:
            cached = self._users.get(user_id)
        if cached:
            return cached

        try:
            profile = self._request("GET", f"/users/{user_id}")
        except MattermostError as error:
            log.warning("Could not resolve Mattermost user %s: %s", user_id, error)
            return BridgeUser(platform=MATTERMOST, user_id=user_id, display_name=user_id)
        return self._remember(profile)

    def _remember(self, profile: dict) -> BridgeUser:
        user_id = profile["id"]
        first = profile.get("first_name", "")
        last = profile.get("last_name", "")
        full = f"{first} {last}".strip()
        user = BridgeUser(
            platform=MATTERMOST,
            user_id=user_id,
            display_name=profile.get("nickname") or full or profile.get("username", user_id),
            username=profile.get("username", ""),
            # Only fetchable with a login, so never usable as a Slack icon_url.
            avatar_url=f"{self.api}/users/{user_id}/image",
            email=profile.get("email", ""),
            is_bot=bool(profile.get("is_bot")),
            real_name=full,
        )
        with self._users_lock:
            self._users[user_id] = user
        return user

    def channel_members(self, channel_id: str) -> List[BridgeUser]:
        """The people in a channel -- no bots, and not the bridge account itself."""
        members: List[BridgeUser] = []
        page = 0
        while True:
            batch = self._request(
                "GET", f"/users?in_channel={channel_id}&per_page=200&page={page}"
            ) or []
            for profile in batch:
                if profile.get("delete_at") or profile.get("is_bot"):
                    continue
                if profile.get("id") == self.bot_user_id:
                    continue
                members.append(self._remember(profile))
            if len(batch) < 200:
                return members
            page += 1

    def user_by_email(self, email: str) -> Optional[BridgeUser]:
        try:
            profile = self._request("GET", f"/users/email/{email}")
        except MattermostError:
            return None
        return self.user(profile["id"])

    def find_user_id_by_name(self, name: str) -> Optional[str]:
        try:
            matches = self._request("POST", "/users/username", json=[name.lstrip("@")])
        except MattermostError:
            return None
        return matches[0]["id"] if matches else None

    # -- inbound -------------------------------------------------------------

    def to_bridge_message(self, post: dict) -> Optional[BridgeMessage]:
        channel = post.get("channel_id", "")
        post_id = post.get("id", "")
        if not channel or not post_id:
            return None

        author = self.user(post.get("user_id", ""))
        root_id = post.get("root_id") or None

        files: List[BridgeFile] = []
        for file_id in post.get("file_ids") or []:
            try:
                info = self._request("GET", f"/files/{file_id}/info")
            except MattermostError:
                log.warning("Could not read metadata for Mattermost file %s", file_id)
                continue
            files.append(
                BridgeFile(
                    file_id=file_id,
                    name=info.get("name", "file"),
                    size=int(info.get("size") or 0),
                    mimetype=info.get("mime_type", ""),
                    source_ref=file_id,
                )
            )

        return BridgeMessage(
            source=MATTERMOST,
            channel_id=channel,
            message_id=post_id,
            author=author,
            text=post.get("message", "") or "",
            thread_root_id=root_id if root_id and root_id != post_id else None,
            files=files,
            raw=post,
        )

    # -- outbound ------------------------------------------------------------

    def post(
        self,
        channel_id: str,
        message: str,
        username: str = "",
        icon_url: str = "",
        root_id: Optional[str] = None,
        file_ids: Optional[List[str]] = None,
    ) -> str:
        props: Dict[str, str] = {}
        if username:
            # Mattermost only honours these when the post is marked as coming
            # from a webhook-style integration, and when the server has
            # EnablePostUsernameOverride / EnablePostIconOverride turned on.
            props["from_webhook"] = "true"
            props["override_username"] = username
            if icon_url:
                props["override_icon_url"] = icon_url

        payload = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        if file_ids:
            payload["file_ids"] = file_ids
        if props:
            payload["props"] = props

        created = self._request("POST", "/posts", json=payload)
        return created["id"]

    def update(self, post_id: str, message: str) -> None:
        self._request("PUT", f"/posts/{post_id}/patch", json={"message": message})

    def delete(self, post_id: str) -> None:
        try:
            self._request("DELETE", f"/posts/{post_id}")
        except MattermostError as error:
            if "404" not in str(error):
                raise

    def add_reaction(self, post_id: str, emoji: str) -> None:
        try:
            self._request(
                "POST",
                "/reactions",
                json={"user_id": self.bot_user_id, "post_id": post_id, "emoji_name": emoji},
            )
        except MattermostError as error:
            log.debug("Could not add reaction %s to %s: %s", emoji, post_id, error)

    def reactors(self, post_id: str, emoji: str) -> List[str]:
        """User IDs currently holding `emoji` on a post."""
        reactions = self._request("GET", f"/posts/{post_id}/reactions") or []
        return [r["user_id"] for r in reactions if r.get("emoji_name") == emoji]

    def remove_reaction(self, post_id: str, emoji: str) -> None:
        try:
            self._request(
                "DELETE", f"/users/{self.bot_user_id}/posts/{post_id}/reactions/{emoji}"
            )
        except MattermostError as error:
            log.debug("Could not remove reaction %s from %s: %s", emoji, post_id, error)

    def upload_file(self, channel_id: str, filename: str, data: bytes) -> Optional[str]:
        response = self._request(
            "POST",
            f"/files?channel_id={channel_id}&filename={requests.utils.quote(filename)}",
            files={"files": (filename, data)},
        )
        infos = (response or {}).get("file_infos") or []
        return infos[0]["id"] if infos else None

    def download(self, bridge_file: BridgeFile) -> Optional[bytes]:
        if bridge_file.size and bridge_file.size > self.config.max_file_bytes:
            log.warning(
                "Skipping Mattermost file %s (%.1f MB exceeds MMBRIDGE_MAX_FILE_MB=%d)",
                bridge_file.name, bridge_file.size / 1e6, self.config.max_file_mb,
            )
            return None
        return self._request("GET", f"/files/{bridge_file.source_ref}")

    # -- connection ----------------------------------------------------------

    def _ws_url(self) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}{parsed.path}/api/v4/websocket"

    def start(self) -> None:
        """Run the event WebSocket, reconnecting with backoff until stopped."""
        thread = threading.Thread(target=self._run_forever, name="mm-websocket", daemon=True)
        thread.start()

    def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                self._connect_once()
                backoff = 1.0
            except Exception as error:
                log.warning("Mattermost WebSocket dropped (%s); reconnecting in %.0fs", error, backoff)
            if self._stopping.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _connect_once(self) -> None:
        url = self._ws_url()

        def on_open(_ws) -> None:
            log.info("Mattermost WebSocket connected (%s)", url)

        def on_message(_ws, raw: str) -> None:
            try:
                payload = json.loads(raw)
            except ValueError:
                return
            if not payload.get("event") or not self.on_event:
                return
            try:
                self.on_event(payload)
            except Exception:
                log.exception("Unhandled error while processing a Mattermost event")

        self._ws = websocket.WebSocketApp(
            url,
            header={"Authorization": f"Bearer {self.config.mattermost_token}"},
            on_open=on_open,
            on_message=on_message,
        )
        # ping_interval keeps the connection alive through proxies that drop
        # idle sockets, which is how a bridge silently stops receiving events.
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def stop(self) -> None:
        self._stopping.set()
        if self._ws:
            self._ws.close()
