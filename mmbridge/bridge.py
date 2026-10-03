"""Routing between the two adapters: loop control, threading, files, reactions."""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Dict, List, Optional, Tuple

from .config import ChannelPair, Config
from .mattermost_adapter import MattermostAdapter
from .mdconv import mattermost_to_slack, slack_to_mattermost, unpinged
from .mentions import ChannelNames
from .models import (
    MATTERMOST,
    SLACK,
    BridgeMessage,
    BridgeUser,
    MessageLink,
)
from .slack_adapter import MESSAGE_SUBTYPES, SlackAdapter
from .store import Store

log = logging.getLogger(__name__)

_SKIN_TONE_RE = re.compile(r"::skin-tone-\d")

# How often to re-check which Slack channels the app is in. The join event
# catches invitations as they happen; the sweep catches the ones made while the
# bridge was down, or whose event never arrived.
CHANNEL_SWEEP_INTERVAL_S = 600


def normalise_emoji(name: str) -> str:
    """Strip modifiers the other platform may not know about."""
    return _SKIN_TONE_RE.sub("", name or "").strip(":")


class Bridge:
    def __init__(self, config: Config, slack=None, mattermost=None) -> None:
        # The adapters are injectable so the routing rules -- loop prevention,
        # threading, direction switches -- can be tested without a network.
        self.config = config
        self.store = Store(config.db_path)

        self.slack_tokens = None
        slack_token = ""
        if config.slack_token_rotation and slack is None:
            from .slack_tokens import SlackTokenManager
            self.slack_tokens = SlackTokenManager(config, self.store)
            slack_token = self.slack_tokens.ensure()

        self.slack = slack or SlackAdapter(config, on_event=self.handle_slack_event, token=slack_token)
        if self.slack_tokens:
            self.slack_tokens.on_refresh = self._switch_slack_token
        self.mattermost = mattermost or MattermostAdapter(
            config, on_event=self.handle_mattermost_event
        )

        # One lock per bridged pair. Slack Socket Mode dispatches on several
        # threads, and two messages mirrored concurrently can land out of order
        # or race each other's thread lookup. Per-pair rather than global so a
        # slow file transfer in one channel does not stall another.
        self._locks: Dict[str, threading.Lock] = {
            pair.slack_channel_id: threading.Lock() for pair in config.pairs
        }

        # Identity override is decided per target, because the two platforms fail
        # differently. Slack honours a per-message username given
        # chat:write.customize. Mattermost ignores override_username outright
        # when the poster is a Bot Account -- it stamps from_bot on every post
        # and renders the bot's own name -- so the author's name would silently
        # vanish. Falling back to the text prefix keeps attribution.
        self.slack_identity_override = config.identity_override
        self.mattermost_identity_override = config.identity_override
        if config.identity_override and getattr(self.mattermost, "is_bot_account", False):
            self.mattermost_identity_override = False
            log.warning(
                "Mattermost is authenticated as a Bot Account, which cannot post under "
                "another author's name: Mattermost stamps from_bot on every post and "
                "ignores override_username. Falling back to prefixing the author's name "
                "into the message text. To show real names in the Mattermost header, "
                "authenticate as an ordinary user account with a personal access token "
                "instead of a Bot Account."
            )

        # Who can be @mentioned across the bridge: the members of each bridged
        # channel, per side, re-read when stale. See mentions.py.
        self._slack_names: Dict[str, ChannelNames] = {
            pair.slack_channel_id: ChannelNames(
                lambda channel=pair.slack_channel_id: self.slack.channel_members(channel),
                f"Slack channel {pair.slack_channel_id}",
            )
            for pair in config.pairs
        }
        self._mm_names: Dict[str, ChannelNames] = {
            pair.mattermost_channel_id: ChannelNames(
                lambda channel=pair.mattermost_channel_id: self.mattermost.channel_members(channel),
                f"Mattermost channel {pair.mattermost_channel_id}",
            )
            for pair in config.pairs
        }

        self._slack_email_index: Optional[Dict[str, BridgeUser]] = None
        self._slack_email_index_failed = False
        self._index_lock = threading.Lock()

        # Unauthorised Slack channels already announced in, so a channel the
        # app cannot leave is told once rather than on every message.
        self._slack_enforced: set = set()
        self._enforce_lock = threading.Lock()
        self._stopping = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for pair in self.config.pairs:
            log.info("Bridging %s", pair)
        log.info(
            "Directions: slack->mm=%s mm->slack=%s | files=%s edits=%s deletes=%s reactions=%s",
            self.config.slack_to_mm, self.config.mm_to_slack,
            self.config.sync_files, self.config.sync_edits,
            self.config.sync_deletes, self.config.sync_reactions,
        )
        if self.slack_tokens:
            log.info("Slack token rotation: on (refreshing %ds before expiry)",
                     self.config.token_refresh_margin_s)
            self.slack_tokens.start()
        self.reconcile_slack_channels()
        threading.Thread(target=self._sweep_loop, name="slack-channel-sweep", daemon=True).start()
        self.mattermost.start()
        self.slack.start()

    def _switch_slack_token(self, token: str) -> None:
        # One client object is shared by the Web API calls, file downloads and
        # the Socket Mode handler, so swapping its token switches all of them.
        self.slack.client.token = token

    def stop(self) -> None:
        self._stopping.set()
        if self.slack_tokens:
            self.slack_tokens.stop()
        self.slack.stop()
        self.mattermost.stop()
        self.store.close()

    # -- identity ------------------------------------------------------------

    def _display_name(self, author: BridgeUser) -> str:
        """The author's name plus a marker saying which platform it came from.

        The marker stays on by default. Making a bridged message indistinguishable
        from a native one would mean people in one organisation cannot tell they
        are talking to someone outside it, which is a worse outcome than a
        slightly longer name.
        """
        suffix = (
            self.config.provenance_suffix_slack
            if author.platform == SLACK
            else self.config.provenance_suffix_mattermost
        )
        return f"{author.display_name} ({suffix})" if suffix else author.display_name

    def _icon_for(self, author: BridgeUser) -> str:
        if author.platform == SLACK:
            # A picture the person chose always wins. Slack avatars are public
            # CDN URLs, so Mattermost can show them. The configured icon only
            # stands in for Slack's generated defaults.
            if author.has_custom_avatar or not self.config.mattermost_icon_url:
                return author.avatar_url
            return self.config.mattermost_icon_url
        # Mattermost avatars need a login to fetch, and Slack fetches anonymously.
        return self.config.slack_fallback_icon_url

    def _slack_email_lookup(self) -> Dict[str, BridgeUser]:
        with self._index_lock:
            if self._slack_email_index is None and not self._slack_email_index_failed:
                try:
                    self._slack_email_index = self.slack.index_users_by_email()
                    log.info("Indexed %d Slack users by email for mention mapping",
                             len(self._slack_email_index))
                except Exception as error:
                    # users:read.email is optional. Without it mentions degrade
                    # to plain text, which is better than failing every message.
                    log.warning(
                        "Could not index Slack users by email (%s); Mattermost "
                        "mentions will be bridged as plain text.", error,
                    )
                    self._slack_email_index_failed = True
            return self._slack_email_index or {}

    def _slack_mention_name(self, slack_user_id: str) -> str:
        """Render a real Slack mention (<@U…>) for Mattermost.

        A real Mattermost mention when the same person has an account there
        (matched by email). Otherwise their name, unpinged: rendered as "@Anna"
        it could notify a Mattermost user who merely has the username "anna".
        """
        user = self.slack.user(slack_user_id)
        if user.email:
            counterpart = self.mattermost.user_by_email(user.email)
            if counterpart and counterpart.username:
                return f"@{counterpart.username}"
        return unpinged(user.display_name or slack_user_id)

    def _mattermost_username_for(self, pair: ChannelPair):
        """Plain "@name" typed in Slack -> a Mattermost member of the pair's channel."""
        names = self._mm_names[pair.mattermost_channel_id]

        def resolve(name: str) -> Optional[str]:
            found = names.lookup(name)
            return found.username if found and found.username else None
        return resolve

    def _slack_id_for(self, pair: ChannelPair):
        """"@name" typed in Mattermost -> a Slack user ID, or None to leave it as typed."""
        names = self._slack_names[pair.slack_channel_id]

        def resolve(name: str) -> Optional[str]:
            # Mattermost usernames are lower-case, and Mattermost itself reads
            # "@Anna" as "@anna". If that is a Mattermost person, it is one --
            # a Slack mention only if they have a Slack account too.
            mm_id = self.mattermost.find_user_id_by_name(name.casefold())
            if mm_id:
                user = self.mattermost.user(mm_id)
                if user.email:
                    match = self._slack_email_lookup().get(user.email.lower())
                    if match:
                        return match.user_id
                return None
            found = names.lookup(name)
            return found.user_id if found else None
        return resolve

    # -- helpers -------------------------------------------------------------

    def _target_thread_root(self, message: BridgeMessage, pair: ChannelPair) -> Optional[str]:
        """The counterpart ID of this message's thread root, if it was bridged.

        A reply whose root never crossed over is posted as a new root rather
        than dropped -- losing the thread shape is better than losing the words.
        """
        if not message.thread_root_id:
            return None
        source_channel = (
            pair.slack_channel_id if message.source == SLACK else pair.mattermost_channel_id
        )
        link = self.store.link_for(message.source, source_channel, message.thread_root_id)
        if not link:
            log.debug("Thread root %s was never bridged; posting as a new root",
                      message.thread_root_id)
            return None
        return link.counterpart(MATTERMOST if message.source == SLACK else SLACK)

    def _transfer_files(self, message: BridgeMessage) -> List[Tuple[str, bytes]]:
        if not self.config.sync_files or not message.files:
            return []
        source = self.slack if message.source == SLACK else self.mattermost
        transferred: List[Tuple[str, bytes]] = []
        for item in message.files:
            try:
                data = source.download(item)
            except Exception:
                log.exception("Could not download %s from %s", item.name, message.source)
                continue
            if data:
                transferred.append((item.name, data))
        return transferred

    def _attribution_prefix(self, author: BridgeUser) -> str:
        """Used when per-message identity override is unavailable or switched off."""
        return f"**{self._display_name(author)}:** "

    # -- mirroring -----------------------------------------------------------

    def mirror_message(self, message: BridgeMessage, pair: ChannelPair) -> None:
        if message.source == SLACK:
            self._slack_to_mattermost(message, pair)
        else:
            self._mattermost_to_slack(message, pair)

    def _slack_to_mattermost(self, message: BridgeMessage, pair: ChannelPair) -> None:
        text = slack_to_mattermost(message.text, resolve_user=self._slack_mention_name,
                                   resolve_plain=self._mattermost_username_for(pair))
        username = self._display_name(message.author) if self.mattermost_identity_override else ""
        if not username:
            text = self._attribution_prefix(message.author) + text

        file_ids: List[str] = []
        for name, data in self._transfer_files(message):
            file_id = self.mattermost.upload_file(pair.mattermost_channel_id, name, data)
            if file_id:
                file_ids.append(file_id)

        if not text and not file_ids:
            return

        post_id = self.mattermost.post(
            channel_id=pair.mattermost_channel_id,
            message=text,
            username=username,
            icon_url=self._icon_for(message.author),
            root_id=self._target_thread_root(message, pair),
            file_ids=file_ids or None,
        )
        self.store.save_link(
            MessageLink(
                slack_channel=pair.slack_channel_id,
                slack_ts=message.message_id,
                slack_thread_ts=message.thread_root_id,
                mattermost_channel=pair.mattermost_channel_id,
                mattermost_post_id=post_id,
                origin=SLACK,
            )
        )
        log.info("slack %s -> mattermost %s (%d file(s))",
                 message.message_id, post_id, len(file_ids))

    def _mattermost_to_slack(self, message: BridgeMessage, pair: ChannelPair) -> None:
        text = mattermost_to_slack(message.text, resolve_mention=self._slack_id_for(pair))
        username = self._display_name(message.author) if self.slack_identity_override else ""
        if not username:
            text = mattermost_to_slack(self._attribution_prefix(message.author)) + text

        files = self._transfer_files(message)
        if not text and not files:
            return
        if not text:
            # Slack needs a message to anchor the upload to, and that message is
            # what carries the author's identity and gets recorded in the link
            # table. Name the attachments rather than post an empty line.
            text = "_" + ", ".join(name for name, _ in files) + "_"

        thread_ts = self._target_thread_root(message, pair)

        if files and self.config.slack_file_style == "inline":
            # One message carrying the file. Slack strips the username override
            # from any message a file is attached to, so the author is named in
            # the text instead -- the trade the "thread" style makes the other way.
            comment = text
            if username:
                comment = mattermost_to_slack(self._attribution_prefix(message.author)) + text
            ts = self.slack.upload_files(
                channel=pair.slack_channel_id,
                files=files,
                thread_ts=thread_ts,
                comment=comment,
            )
            if not ts:
                log.warning(
                    "Uploaded %d file(s) to Slack but could not determine the message "
                    "timestamp; this message will not be linked for edits or replies.",
                    len(files),
                )
                return
        else:
            ts = self.slack.post(
                channel=pair.slack_channel_id,
                text=text,
                username=username,
                icon_url=self._icon_for(message.author),
                thread_ts=thread_ts,
                # Only genuine replies. A reply whose root never crossed over
                # arrives as a new root, and the thread that "thread" file style
                # creates under a root message is not a conversation to surface.
                broadcast=bool(thread_ts) and self.config.slack_broadcast_replies,
            )
            if files:
                # files_upload_v2 always posts as the app, so the files hang under
                # the author's own message rather than beside it unattributed.
                try:
                    self.slack.upload_files(
                        channel=pair.slack_channel_id,
                        files=files,
                        thread_ts=thread_ts or ts,
                    )
                except Exception:
                    log.exception("Could not upload %d file(s) to Slack", len(files))

        self.store.save_link(
            MessageLink(
                slack_channel=pair.slack_channel_id,
                slack_ts=ts,
                slack_thread_ts=thread_ts,
                mattermost_channel=pair.mattermost_channel_id,
                mattermost_post_id=message.message_id,
                mattermost_root_id=message.thread_root_id,
                origin=MATTERMOST,
            )
        )
        log.info("mattermost %s -> slack %s (%d file(s))",
                 message.message_id, ts, len(files))

    # -- Slack events --------------------------------------------------------

    # -- Slack channel allowlist enforcement ---------------------------------

    def enforce_slack_channel(self, channel: str, name: str = "") -> None:
        """Leave a Slack channel that is not on the allowlist.

        Nothing from such a channel is ever forwarded -- the pair lookup already
        drops it. Leaving goes further: Slack stops streaming that channel's
        messages to the bridge at all, so they are never even received, and
        whoever invited the app sees immediately that it does not belong there.
        """
        # Only channels (C…) and private channels (G…) can be left; a DM cannot.
        if not channel or not channel.startswith(("C", "G")):
            return
        if self.config.pair_for_slack(channel):
            return

        with self._enforce_lock:
            first_time = channel not in self._slack_enforced
            self._slack_enforced.add(channel)
        label = f"#{name}" if name else channel

        if not self.config.slack_leave_unauthorised:
            if first_time:
                log.warning("Ignoring unauthorised Slack channel %s (automatic leaving is off)", label)
            return

        if first_time and self.config.slack_leave_notice:
            try:
                self.slack.post_as_app(channel, self.config.slack_leave_notice)
            except Exception:
                log.exception("Could not post the unauthorised-channel notice in %s", label)

        outcome = self.slack.leave(channel)
        if outcome == "left":
            if first_time:
                log.warning("Left unauthorised Slack channel %s", label)
        elif first_time:
            hint = (
                " Grant channels:manage (public) and groups:write (private) and reinstall "
                "the app to allow automatic leaving."
                if outcome == "missing_scope" else ""
            )
            log.warning(
                "Could not leave unauthorised Slack channel %s (%s). Nothing from it is "
                "forwarded, but Slack keeps delivering its messages until someone removes "
                "the app.%s", label, outcome, hint,
            )

    def reconcile_slack_channels(self) -> None:
        """Leave every Slack channel the app is in that is not on the allowlist.

        Refuses to act if any configured channel cannot be found. A typo in
        MMBRIDGE_PAIRS would otherwise make the bridge treat its real channel as
        unauthorised and walk out of it on the next start.
        """
        if not self.config.slack_leave_unauthorised:
            return
        try:
            unresolved = [
                pair.slack_channel_id
                for pair in self.config.pairs
                if not self.slack.channel_exists(pair.slack_channel_id)
            ]
            if unresolved:
                log.error(
                    "Skipping the Slack channel sweep: configured channel(s) %s cannot be "
                    "found in this workspace. Check MMBRIDGE_PAIRS -- the sweep will not "
                    "leave anything while the allowlist looks wrong.",
                    ", ".join(unresolved),
                )
                return
            for channel in self.slack.member_channels():
                if not self.config.pair_for_slack(channel["id"]):
                    self.enforce_slack_channel(channel["id"], channel.get("name", ""))
        except Exception:
            log.exception("Slack channel sweep failed; it will be retried")

    def _sweep_loop(self) -> None:
        while not self._stopping.wait(CHANNEL_SWEEP_INTERVAL_S):
            self.reconcile_slack_channels()

    def handle_slack_event(self, payload: dict) -> None:
        event = payload.get("event") or {}
        event_type = event.get("type")
        channel = event.get("channel", "")

        # Allowlist enforcement runs before the direction check. It is a
        # security control rather than a mirroring feature, so it must hold for
        # a one-way mm_to_slack deployment too.
        if event_type == "member_joined_channel":
            if event.get("user") == self.slack.bot_user_id:
                # A fresh invitation: announce again even if this channel was
                # left once before.
                with self._enforce_lock:
                    self._slack_enforced.discard(channel)
                self.enforce_slack_channel(channel)
            return
        if event_type == "message" and channel and not self.config.pair_for_slack(channel):
            # Receiving a message proves the app is a member of this channel.
            self.enforce_slack_channel(channel)
            return

        if not self.config.slack_to_mm:
            return

        if event_type == "message":
            self._on_slack_message(payload, event)
        elif event_type in {"reaction_added", "reaction_removed"} and self.config.sync_reactions:
            self._on_slack_reaction(payload, event, added=event_type == "reaction_added")

    def _is_own_slack_message(self, event: dict) -> bool:
        return bool(
            (self.slack.bot_id and event.get("bot_id") == self.slack.bot_id)
            or event.get("user") == self.slack.bot_user_id
        )

    def _on_slack_message(self, payload: dict, event: dict) -> None:
        subtype = event.get("subtype")
        channel = event.get("channel", "")
        pair = self.config.pair_for_slack(channel)
        if not pair:
            return

        with self._locks[pair.slack_channel_id]:
            if subtype == "message_changed":
                self._on_slack_edit(pair, event)
                return
            if subtype == "message_deleted":
                self._on_slack_delete(pair, event)
                return
            if subtype not in MESSAGE_SUBTYPES:
                return
            if self._is_own_slack_message(event):
                return

            ts = event.get("ts", "")
            # The definitive loop guard: this timestamp is already one half of a
            # link, so the bridge itself created it.
            if self.store.link_by_slack(channel, ts):
                return
            if not self.store.mark_seen(f"slack:{payload.get('event_id') or ts}"):
                return

            message = self.slack.to_bridge_message(event)
            if message:
                self.mirror_message(message, pair)

    def _on_slack_edit(self, pair: ChannelPair, event: dict) -> None:
        if not self.config.sync_edits:
            return
        inner = event.get("message") or {}
        previous = event.get("previous_message") or {}
        if self._is_own_slack_message(inner):
            return
        if inner.get("text", "") == previous.get("text", ""):
            return

        link = self.store.link_by_slack(pair.slack_channel_id, inner.get("ts", ""))
        if not link or link.origin != SLACK:
            return
        text = slack_to_mattermost(inner.get("text", ""), resolve_user=self._slack_mention_name,
                                   resolve_plain=self._mattermost_username_for(pair))
        if not self.mattermost_identity_override:
            text = self._attribution_prefix(self.slack.user(inner.get("user", ""))) + text
        self.mattermost.update(link.mattermost_post_id, text)
        log.info("edit slack %s -> mattermost %s", link.slack_ts, link.mattermost_post_id)

    def _on_slack_delete(self, pair: ChannelPair, event: dict) -> None:
        if not self.config.sync_deletes:
            return
        link = self.store.link_by_slack(pair.slack_channel_id, event.get("deleted_ts", ""))
        if not link or link.origin != SLACK:
            return
        self.mattermost.delete(link.mattermost_post_id)
        self.store.delete_link(link)
        log.info("delete slack %s -> mattermost %s", link.slack_ts, link.mattermost_post_id)

    def _on_slack_reaction(self, payload: dict, event: dict, added: bool) -> None:
        if event.get("user") == self.slack.bot_user_id:
            return
        reaction = self.slack.to_bridge_reaction(event)
        if not reaction:
            return
        pair = self.config.pair_for_slack(reaction.channel_id)
        if not pair:
            return
        link = self.store.link_by_slack(reaction.channel_id, reaction.message_id)
        if not link:
            return
        emoji = normalise_emoji(reaction.emoji)
        if added:
            self.mattermost.add_reaction(link.mattermost_post_id, emoji)
        elif not self._others_still_react(
            lambda: self.slack.reactors(reaction.channel_id, reaction.message_id, emoji),
            self.slack.bot_user_id,
        ):
            self.mattermost.remove_reaction(link.mattermost_post_id, emoji)

    @staticmethod
    def _others_still_react(lookup, bridge_user_id: str) -> bool:
        """Whether anyone but the bridge still holds the emoji on the original.

        The bridge account can hold each emoji only once on the far side, so it
        stands in for every reactor at once. It may only take its reaction back
        when the last of them has. If the lookup fails, behave as before and
        remove: a lingering reaction nobody can explain is worse than a briefly
        missing one.
        """
        try:
            return any(user != bridge_user_id for user in lookup())
        except Exception:
            log.warning("Could not read remaining reactions; removing the mirrored one", exc_info=True)
            return False

    # -- Mattermost events ---------------------------------------------------

    def handle_mattermost_event(self, payload: dict) -> None:
        if not self.config.mm_to_slack:
            return
        event = payload.get("event")
        data = payload.get("data") or {}

        if event == "posted":
            self._on_mattermost_post(data)
        elif event == "post_edited" and self.config.sync_edits:
            self._on_mattermost_edit(data)
        elif event == "post_deleted" and self.config.sync_deletes:
            self._on_mattermost_delete(data)
        elif event in {"reaction_added", "reaction_removed"} and self.config.sync_reactions:
            self._on_mattermost_reaction(data, added=event == "reaction_added")

    @staticmethod
    def _post_from(data: dict) -> Optional[dict]:
        raw = data.get("post")
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _on_mattermost_post(self, data: dict) -> None:
        post = self._post_from(data)
        if not post:
            return
        pair = self.config.pair_for_mattermost(post.get("channel_id", ""))
        if not pair:
            return
        # Mattermost reports joins, leaves and system notices as posts with a
        # non-empty type; only ordinary posts have an empty one.
        if post.get("type"):
            return
        if post.get("user_id") == self.mattermost.bot_user_id:
            return

        with self._locks[pair.slack_channel_id]:
            if self.store.link_by_mattermost(pair.mattermost_channel_id, post.get("id", "")):
                return
            if not self.store.mark_seen(f"mm:{post.get('id')}"):
                return
            message = self.mattermost.to_bridge_message(post)
            if message:
                self.mirror_message(message, pair)

    def _on_mattermost_edit(self, data: dict) -> None:
        post = self._post_from(data)
        if not post or post.get("user_id") == self.mattermost.bot_user_id:
            return
        pair = self.config.pair_for_mattermost(post.get("channel_id", ""))
        if not pair:
            return
        link = self.store.link_by_mattermost(pair.mattermost_channel_id, post.get("id", ""))
        if not link or link.origin != MATTERMOST:
            return
        text = mattermost_to_slack(post.get("message", ""), resolve_mention=self._slack_id_for(pair))
        if not self.slack_identity_override:
            text = mattermost_to_slack(
                self._attribution_prefix(self.mattermost.user(post.get("user_id", "")))
            ) + text
        self.slack.update(link.slack_channel, link.slack_ts, text)
        log.info("edit mattermost %s -> slack %s", link.mattermost_post_id, link.slack_ts)

    def _on_mattermost_delete(self, data: dict) -> None:
        post = self._post_from(data)
        if not post:
            return
        pair = self.config.pair_for_mattermost(post.get("channel_id", ""))
        if not pair:
            return
        link = self.store.link_by_mattermost(pair.mattermost_channel_id, post.get("id", ""))
        if not link or link.origin != MATTERMOST:
            return
        self.slack.delete(link.slack_channel, link.slack_ts)
        self.store.delete_link(link)
        log.info("delete mattermost %s -> slack %s", link.mattermost_post_id, link.slack_ts)

    def _on_mattermost_reaction(self, data: dict, added: bool) -> None:
        raw = data.get("reaction")
        if not raw:
            return
        try:
            reaction = raw if isinstance(raw, dict) else json.loads(raw)
        except ValueError:
            return
        if reaction.get("user_id") == self.mattermost.bot_user_id:
            return

        post_id = reaction.get("post_id", "")
        link = next(
            (
                found
                for pair in self.config.pairs
                if (found := self.store.link_by_mattermost(pair.mattermost_channel_id, post_id))
            ),
            None,
        )
        if not link:
            return
        emoji = normalise_emoji(reaction.get("emoji_name", ""))
        if added:
            self.slack.add_reaction(link.slack_channel, link.slack_ts, emoji)
        elif not self._others_still_react(
            lambda: self.mattermost.reactors(post_id, emoji), self.mattermost.bot_user_id,
        ):
            self.slack.remove_reaction(link.slack_channel, link.slack_ts, emoji)
