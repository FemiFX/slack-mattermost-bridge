"""Configuration loading for mmbridge.

Values come from the process environment, optionally seeded from an env file.
Follows the same convention as tbsync: a dedicated ``.env.mmbridge`` file so the
bridge never picks up credentials belonging to another stack.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_ENV_FILE = ".env.mmbridge"

# Posted once into a Slack channel the app was invited to without authorisation,
# just before it leaves. Worded neutrally so it holds whether or not the leave
# succeeds (it cannot without channels:manage / groups:write).
DEFAULT_LEAVE_NOTICE = (
    "Diese App ist nur für einen freigegebenen Channel autorisiert; Nachrichten "
    "aus diesem Channel werden nicht weitergeleitet. / This app is only "
    "authorised for one approved channel; nothing from this channel is forwarded."
)


class ConfigError(RuntimeError):
    pass


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _split(value: str) -> List[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _secret(name: str) -> str:
    """Read a secret from NAME, or from the file named by NAME_FILE.

    Docker and Swarm secrets are mounted as files under /run/secrets, so every
    credential can be supplied either way. NAME wins if both are set.
    """
    value = os.environ.get(name, "")
    if value:
        return value.strip()
    path = os.environ.get(f"{name}_FILE", "")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as error:
        raise ConfigError(f"{name}_FILE points at {path!r}, which cannot be read: {error.strerror}")


def load_env_file(path: str = DEFAULT_ENV_FILE) -> None:
    """Seed os.environ from a KEY=VALUE file. Existing env vars win."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class ChannelPair:
    """One bridged conversation.

    Pairs are an explicit allowlist rather than a wildcard. The bridge moves
    messages and files between two security domains, so which channels are
    connected has to be a deliberate, auditable decision -- never inferred from
    whatever the app happens to have been invited to.
    """

    slack_channel_id: str
    mattermost_channel_id: str
    label: str = ""

    @classmethod
    def parse(cls, raw: str) -> "ChannelPair":
        parts = [p.strip() for p in raw.split(":")]
        if len(parts) == 2:
            slack_id, mm_id, label = parts[0], parts[1], ""
        elif len(parts) == 3:
            slack_id, mm_id, label = parts
        else:
            raise ConfigError(
                f"Cannot parse channel pair {raw!r}. Expected "
                "'SLACK_CHANNEL_ID:MATTERMOST_CHANNEL_ID' with an optional "
                "':label' suffix, e.g. 'C0123ABCDEF:bx8ck7…:partner-project'."
            )
        if not slack_id or not mm_id:
            raise ConfigError(f"Channel pair {raw!r} is missing one of the two channel IDs.")
        return cls(slack_channel_id=slack_id, mattermost_channel_id=mm_id, label=label)

    def __str__(self) -> str:
        name = self.label or "pair"
        return f"{name}[slack:{self.slack_channel_id} <-> mm:{self.mattermost_channel_id}]"


@dataclass
class Config:
    slack_bot_token: str
    slack_app_token: str

    mattermost_url: str
    mattermost_token: str

    pairs: List[ChannelPair] = field(default_factory=list)

    db_path: str = "mmbridge.sqlite3"

    # Feature switches. Everything a message can carry is bridged only if it is
    # switched on here, so a deployment can start narrow and widen later.
    sync_files: bool = True
    sync_edits: bool = True
    sync_deletes: bool = True
    sync_reactions: bool = True

    # Post as the originating person's name and avatar. Requires
    # EnablePostUsernameOverride and EnablePostIconOverride on the Mattermost
    # side, and the chat:write.customize scope on the Slack side. When off, the
    # bridge falls back to prefixing the author's name into the message text.
    identity_override: bool = True

    # Provenance marker appended to the display name, e.g. "Anna (via Slack)".
    # Kept on by default: people should always be able to tell that a message
    # reached them through an integration rather than natively.
    provenance_suffix_slack: str = "via Slack"
    provenance_suffix_mattermost: str = "via Mattermost"

    # Icon Slack shows beside messages from Mattermost authors. Their own
    # avatars cannot be used: Mattermost serves them only to logged-in clients
    # (an anonymous fetch gets 401, however public the server), and Slack
    # fetches icon URLs anonymously. Empty means Slack shows the app's icon.
    slack_fallback_icon_url: str = ""
    # Icon Mattermost shows beside messages from Slack authors who have no
    # picture of their own (nothing uploaded to Slack, no Gravatar). Anyone who
    # chose a picture keeps it. Empty means Slack's generated default.
    mattermost_icon_url: str = ""

    # How a Mattermost file appears in Slack. Slack will not give both the
    # author's name in the header and the file inside the same message:
    # attaching a file to a chat.postMessage strips the username override.
    #   "thread" - author's name on the message, files threaded beneath it
    #   "inline" - one message carrying the file, author named in the text
    slack_file_style: str = "thread"

    # Show replies coming from Mattermost in the Slack channel as well as in
    # their thread ("Also send to #channel"). Slack always collapses threads
    # behind "1 reply", with no setting to show them inline, whereas Mattermost
    # users often read replies in the channel -- so without this, replies from
    # the Mattermost side are easy to miss in Slack.
    slack_broadcast_replies: bool = True

    # Leave any Slack channel the app is invited to that is not in
    # MMBRIDGE_PAIRS. Nothing from such a channel is forwarded either way; leaving
    # additionally stops Slack streaming its messages to the bridge at all.
    slack_leave_unauthorised: bool = True
    slack_leave_notice: str = DEFAULT_LEAVE_NOTICE

    # Token rotation. On when both client credentials are set. The bot token then
    # lives in the database (encrypted with token_key) and is refreshed before
    # it expires; SLACK_BOT_TOKEN / SLACK_REFRESH_TOKEN are only needed once, to
    # bootstrap, and can be removed afterwards.
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_refresh_token: str = ""
    token_key: str = ""
    # Refresh this many seconds before the token expires. Slack issues 12-hour
    # tokens; the default leaves an hour to retry through a network outage.
    # Raise it close to 43200 to watch several refreshes in a test.
    token_refresh_margin_s: int = 3600

    max_file_mb: int = 50
    direction: str = "both"  # both | slack_to_mm | mm_to_slack
    log_level: str = "INFO"

    @property
    def slack_token_rotation(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret)

    @property
    def slack_to_mm(self) -> bool:
        return self.direction in {"both", "slack_to_mm"}

    @property
    def mm_to_slack(self) -> bool:
        return self.direction in {"both", "mm_to_slack"}

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def pair_for_slack(self, channel_id: str) -> Optional[ChannelPair]:
        for pair in self.pairs:
            if pair.slack_channel_id == channel_id:
                return pair
        return None

    def pair_for_mattermost(self, channel_id: str) -> Optional[ChannelPair]:
        for pair in self.pairs:
            if pair.mattermost_channel_id == channel_id:
                return pair
        return None

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get

        slack_client_id = env("SLACK_CLIENT_ID", "").strip()
        slack_client_secret = _secret("SLACK_CLIENT_SECRET")
        if bool(slack_client_id) != bool(slack_client_secret):
            raise ConfigError(
                "Token rotation needs both SLACK_CLIENT_ID and SLACK_CLIENT_SECRET "
                "(Basic Information -> App Credentials). Only one of them is set."
            )
        rotation = bool(slack_client_id)

        secrets = {
            "SLACK_BOT_TOKEN": _secret("SLACK_BOT_TOKEN"),
            "SLACK_APP_TOKEN": _secret("SLACK_APP_TOKEN"),
            "MATTERMOST_TOKEN": _secret("MATTERMOST_TOKEN"),
        }
        # With rotation the bot token lives in the database after the first
        # run, so SLACK_BOT_TOKEN is only needed to bootstrap and is checked
        # there instead.
        required = ["SLACK_APP_TOKEN", "MATTERMOST_URL", "MATTERMOST_TOKEN"]
        if not rotation:
            required.insert(0, "SLACK_BOT_TOKEN")
        missing = [
            name for name in required
            if not (secrets.get(name) if name in secrets else env(name))
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + f"\nSet them in the shell or in {DEFAULT_ENV_FILE} (see .env.mmbridge.example)."
            )

        raw_pairs = _split(env("MMBRIDGE_PAIRS", ""))
        if not raw_pairs:
            raise ConfigError(
                "MMBRIDGE_PAIRS is empty. The bridge only touches channels named "
                "there, so at least one 'SLACK_CHANNEL_ID:MATTERMOST_CHANNEL_ID' "
                "pair is required."
            )
        pairs = [ChannelPair.parse(raw) for raw in raw_pairs]

        for attr in ("slack_channel_id", "mattermost_channel_id"):
            seen = [getattr(p, attr) for p in pairs]
            duplicate = next((c for c in seen if seen.count(c) > 1), None)
            if duplicate:
                raise ConfigError(
                    f"Channel {duplicate} appears in more than one pair. A channel can "
                    "only be bridged to one counterpart -- two pairs sharing a channel "
                    "would echo every message between them."
                )

        direction = env("MMBRIDGE_DIRECTION", "both").strip().lower()
        if direction not in {"both", "slack_to_mm", "mm_to_slack"}:
            raise ConfigError(
                f"MMBRIDGE_DIRECTION={direction!r} is not valid. "
                "Use 'both', 'slack_to_mm' or 'mm_to_slack'."
            )

        bot_token = secrets["SLACK_BOT_TOKEN"]
        if bot_token.startswith("xoxe.") and not rotation:
            raise ConfigError(
                "SLACK_BOT_TOKEN is a rotating token (it starts with 'xoxe.'), which "
                "expires after 12 hours. Set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET "
                "so the bridge can refresh it, plus SLACK_REFRESH_TOKEN if you have one."
            )

        token_key = _secret("MMBRIDGE_TOKEN_KEY")
        if rotation:
            if not token_key:
                raise ConfigError(
                    "Token rotation stores the Slack token in the database, encrypted "
                    "with MMBRIDGE_TOKEN_KEY (or MMBRIDGE_TOKEN_KEY_FILE), which is not "
                    "set. Generate one with: python -m mmbridge genkey"
                )
            from .slack_tokens import validate_key
            problem = validate_key(token_key)
            if problem:
                raise ConfigError(f"MMBRIDGE_TOKEN_KEY is not usable: {problem}")

        margin = int(env("MMBRIDGE_TOKEN_REFRESH_MARGIN", "3600"))
        if not 60 <= margin <= 43140:
            raise ConfigError(
                "MMBRIDGE_TOKEN_REFRESH_MARGIN must be between 60 and 43140 seconds "
                "(Slack tokens live 43200 seconds)."
            )

        slack_app_token = secrets["SLACK_APP_TOKEN"]
        if not slack_app_token.startswith("xapp-"):
            raise ConfigError(
                "SLACK_APP_TOKEN does not look like an app-level token (expected an "
                "'xapp-' prefix). Socket Mode needs the app-level token from "
                "'Basic Information -> App-Level Tokens' with the connections:write "
                "scope -- not the xoxb- bot token."
            )

        slack_file_style = env("MMBRIDGE_SLACK_FILE_STYLE", "thread").strip().lower()
        if slack_file_style not in {"thread", "inline"}:
            raise ConfigError(
                f"MMBRIDGE_SLACK_FILE_STYLE={slack_file_style!r} is not valid. Use "
                "'thread' (author's name on the message, files threaded beneath) or "
                "'inline' (one message carrying the file, author named in the text)."
            )

        return cls(
            slack_bot_token=bot_token,
            slack_app_token=slack_app_token,
            mattermost_url=env("MATTERMOST_URL", "").rstrip("/"),
            mattermost_token=secrets["MATTERMOST_TOKEN"],
            slack_client_id=slack_client_id,
            slack_client_secret=slack_client_secret,
            slack_refresh_token=_secret("SLACK_REFRESH_TOKEN"),
            token_key=token_key,
            token_refresh_margin_s=margin,
            pairs=pairs,
            db_path=env("MMBRIDGE_DB", "mmbridge.sqlite3"),
            sync_files=_bool(env("MMBRIDGE_SYNC_FILES"), True),
            sync_edits=_bool(env("MMBRIDGE_SYNC_EDITS"), True),
            sync_deletes=_bool(env("MMBRIDGE_SYNC_DELETES"), True),
            sync_reactions=_bool(env("MMBRIDGE_SYNC_REACTIONS"), True),
            identity_override=_bool(env("MMBRIDGE_IDENTITY_OVERRIDE"), True),
            provenance_suffix_slack=env("MMBRIDGE_PROVENANCE_SLACK", "via Slack"),
            provenance_suffix_mattermost=env("MMBRIDGE_PROVENANCE_MATTERMOST", "via Mattermost"),
            slack_fallback_icon_url=env("MMBRIDGE_SLACK_ICON_URL", ""),
            mattermost_icon_url=env("MMBRIDGE_MATTERMOST_ICON_URL", "").strip(),
            slack_file_style=slack_file_style,
            slack_broadcast_replies=_bool(env("MMBRIDGE_SLACK_BROADCAST_REPLIES"), True),
            slack_leave_unauthorised=_bool(env("MMBRIDGE_SLACK_LEAVE_UNAUTHORISED"), True),
            slack_leave_notice=env("MMBRIDGE_SLACK_LEAVE_NOTICE", DEFAULT_LEAVE_NOTICE),
            max_file_mb=int(env("MMBRIDGE_MAX_FILE_MB", "50")),
            direction=direction,
            log_level=env("MMBRIDGE_LOG_LEVEL", "INFO").upper(),
        )
