"""Slack token rotation: bootstrap, encrypted storage, and refresh.

With rotation switched on for the app, Slack bot tokens expire after 12 hours.
A single-use refresh token buys the next pair. This module owns that cycle:

1. **Bootstrap, once.** Either exchange the long-lived SLACK_BOT_TOKEN issued
   before rotation was switched on (``oauth.v2.exchange``), or -- for an app
   installed after opting in -- spend a SLACK_REFRESH_TOKEN copied from the app
   settings. Either way the result is an access token plus a refresh token.

2. **Persist before use.** Every new pair is written to the database in one
   transaction *before* the bridge switches to it. Refresh tokens are
   single-use; a crash between receiving a pair and saving it is the one way to
   lock the bridge out until the app is reinstalled, so that window is kept to a
   single network round trip.

3. **Refresh ahead of expiry**, retrying with backoff while the current token is
   still valid.

Tokens are encrypted at rest with MMBRIDGE_TOKEN_KEY, so a copied database or a
backup is useless on its own. Be clear about the limit, though: the key, the
client secret and the refresh token all live on the host that runs the bridge,
so none of this protects against someone who has taken over that host.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Callable, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

# How often the refresh loop wakes to look for a newer pair in the database --
# one written by `mmbridge refresh-token`, say. Adopting it matters: that
# command spent the refresh token this process is holding.
STORE_POLL_INTERVAL_S = 300

# Guidance for the refresh failures that retrying will not fix.
_HINTS = {
    "invalid_refresh_token": (
        "Slack no longer accepts the stored refresh token. If this persists, the app "
        "must be reinstalled and a fresh SLACK_BOT_TOKEN or SLACK_REFRESH_TOKEN supplied."
    ),
    "invalid_client_id": "SLACK_CLIENT_ID is wrong for this app.",
    "bad_client_secret": "SLACK_CLIENT_SECRET is wrong, or was regenerated in the app settings.",
    "token_rotation_not_enabled": (
        "Token rotation is not switched on for this app yet (OAuth & Permissions -> "
        "Advanced token security via token rotation)."
    ),
}


class TokenError(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def validate_key(key: str) -> Optional[str]:
    """Return a problem description, or None if `key` is a usable Fernet key."""
    try:
        Fernet(key.encode())
        return None
    except (ValueError, TypeError):
        return (
            "expected 32 url-safe base64-encoded bytes, as produced by "
            "`python -m mmbridge genkey`"
        )


def fingerprint(*secrets: str) -> str:
    """A one-way identifier for the bootstrap values, so a changed value is noticed."""
    if not any(secrets):
        return ""
    return hashlib.sha256("\0".join(secrets).encode()).hexdigest()


def describe(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}h {rest // 60:02d}m" if hours else f"{rest // 60}m {rest % 60:02d}s"


def _error_code(error: Exception) -> str:
    if isinstance(error, SlackApiError):
        return error.response.get("error", "unknown_error")
    return type(error).__name__


class SlackOAuth:
    """The Slack calls rotation needs, behind one seam so tests can replace it.

    The SDK's convenience wrappers put the client secret and refresh token in the
    URL query string. URLs turn up in debug and proxy logs far more readily than
    request bodies do, so both calls send them in the POST body instead.
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = WebClient()

    def exchange(self, legacy_token: str) -> dict:
        return self._client.api_call(
            "oauth.v2.exchange",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "token": legacy_token,
            },
        ).data

    def refresh(self, refresh_token: str) -> dict:
        return self._client.api_call(
            "oauth.v2.access",
            auth={"client_id": self._client_id, "client_secret": self._client_secret},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        ).data

    def token_works(self, token: str) -> bool:
        try:
            WebClient(token=token).auth_test()
            return True
        except SlackApiError:
            return False


class SlackTokenManager:
    def __init__(self, config, store, oauth: Optional[SlackOAuth] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.store = store
        self.oauth = oauth or SlackOAuth(config.slack_client_id, config.slack_client_secret)
        self._fernet = Fernet(config.token_key.encode())
        self._clock = clock

        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._access = ""
        self._refresh = ""
        self._expires_at = 0.0
        self._fingerprint = ""

        # Called with each new access token, so the Slack client can switch.
        self.on_refresh: Optional[Callable[[str], None]] = None
        self._stopping = threading.Event()

    # -- state ---------------------------------------------------------------

    @property
    def access_token(self) -> str:
        with self._state_lock:
            return self._access

    @property
    def seconds_left(self) -> float:
        with self._state_lock:
            return self._expires_at - self._clock()

    @property
    def bootstrap_fingerprint(self) -> str:
        return fingerprint(self.config.slack_bot_token, self.config.slack_refresh_token)

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            raise TokenError(
                "The stored Slack token cannot be decrypted with MMBRIDGE_TOKEN_KEY -- the "
                "key is not the one it was stored with. Restore the original key, or "
                "bootstrap again by supplying a fresh SLACK_BOT_TOKEN or SLACK_REFRESH_TOKEN."
            ) from None

    def _set(self, access: str, refresh: str, expires_at: float, fp: str, notify: bool) -> None:
        with self._state_lock:
            self._access, self._refresh = access, refresh
            self._expires_at, self._fingerprint = expires_at, fp
        if notify and self.on_refresh:
            self.on_refresh(access)

    def _adopt(self, row: dict, notify: bool = False) -> None:
        self._set(
            self._decrypt(row["access_token"]),
            self._decrypt(row["refresh_token"]),
            float(row["expires_at"]),
            row["bootstrap_fingerprint"],
            notify,
        )

    def _persist_and_set(self, access: str, refresh: str, expires_at: float, fp: str) -> None:
        # Save first. If the process dies after this line, the next start has
        # the new pair; if it dies before, Slack's grace period on the old
        # refresh token covers a quick restart.
        self.store.save_slack_tokens(self._encrypt(access), self._encrypt(refresh), expires_at, fp)
        self._set(access, refresh, expires_at, fp, notify=True)

    def _grant(self, response: dict, source: str) -> Tuple[str, str, float]:
        access = response.get("access_token")
        refresh = response.get("refresh_token")
        expires_in = response.get("expires_in")
        if not (access and refresh and expires_in):
            fields = ", ".join(sorted(response))   # names only, never values
            raise TokenError(
                f"Slack's {source} response did not contain a rotating token pair "
                f"(fields present: {fields}). Check that token rotation is switched on "
                "for the app."
            )
        return access, refresh, self._clock() + float(expires_in)

    # -- lifecycle -----------------------------------------------------------

    def ensure(self) -> str:
        """Return a usable access token, bootstrapping or refreshing first if needed."""
        row = self.store.load_slack_tokens()
        env_fp = self.bootstrap_fingerprint

        # Stored tokens win -- unless the bootstrap values in the environment
        # changed, which means someone supplied new ones on purpose (after a
        # reinstall, typically) and wants them used.
        if row and (not env_fp or env_fp == row["bootstrap_fingerprint"]):
            self._adopt(row)
            log.info("Using the stored rotating Slack token (expires in %s)", describe(self.seconds_left))
        else:
            if not env_fp:
                raise TokenError(
                    "Token rotation is on, but there is no stored Slack token and nothing to "
                    "bootstrap from. Set SLACK_BOT_TOKEN (a long-lived xoxb- token, which is "
                    "exchanged) or SLACK_REFRESH_TOKEN (from an app installed after rotation "
                    "was switched on)."
                )
            if row:
                log.info("SLACK_BOT_TOKEN / SLACK_REFRESH_TOKEN changed since the stored token "
                         "was issued; bootstrapping again from the new values")
            self._bootstrap(env_fp)

        if self.seconds_left <= self.config.token_refresh_margin_s:
            self.refresh()
        return self.access_token

    def _bootstrap(self, env_fp: str) -> None:
        bot = self.config.slack_bot_token
        refresh = self.config.slack_refresh_token
        try:
            if refresh:
                # Issued after opting in: one refresh yields a fresh pair and
                # proves the client credentials in the same step.
                granted = self._grant(self.oauth.refresh(refresh), "refresh")
                source = "SLACK_REFRESH_TOKEN"
            elif bot.startswith("xoxe."):
                raise TokenError(
                    "SLACK_BOT_TOKEN is already a rotating token, but SLACK_REFRESH_TOKEN is not "
                    "set. A rotating token cannot be renewed without its refresh token; copy it "
                    "from the app's OAuth & Permissions page."
                )
            else:
                granted = self._grant(self.oauth.exchange(bot), "exchange")
                source = "SLACK_BOT_TOKEN (exchanged)"
        except SlackApiError as error:
            code = _error_code(error)
            raise TokenError(f"Bootstrapping the rotating Slack token failed: {code}. "
                             + _HINTS.get(code, "")) from None

        self._persist_and_set(*granted, env_fp)
        log.info("Bootstrapped the rotating Slack token from %s; it expires in %s",
                 source, describe(self.seconds_left))

        if not refresh:
            if self.oauth.token_works(bot):
                log.warning(
                    "The original long-lived SLACK_BOT_TOKEN still works after the exchange. "
                    "Rotation protects nothing while it does: revoke it and remove it from "
                    "the configuration."
                )
            else:
                log.info("The original long-lived SLACK_BOT_TOKEN no longer works, as intended. "
                         "It can be removed from the configuration.")

    def refresh(self, force: bool = False) -> None:
        with self._refresh_lock:
            # Another process may have refreshed already -- its refresh token
            # supersedes ours, which it has just spent.
            row = self.store.load_slack_tokens()
            with self._state_lock:
                current_expiry = self._expires_at
            if row and float(row["expires_at"]) > current_expiry:
                self._adopt(row, notify=True)
                if not force and self.seconds_left > self.config.token_refresh_margin_s:
                    return

            with self._state_lock:
                refresh, fp = self._refresh, self._fingerprint
            try:
                granted = self._grant(self.oauth.refresh(refresh), "refresh")
            except SlackApiError as error:
                code = _error_code(error)
                raise TokenError(f"{code}. {_HINTS.get(code, '')}".strip()) from None
            self._persist_and_set(*granted, fp)
            log.info("Refreshed the Slack token; the new one expires in %s", describe(self.seconds_left))

    def _adopt_newer_from_store(self) -> None:
        row = self.store.load_slack_tokens()
        with self._state_lock:
            current_expiry = self._expires_at
        if row and float(row["expires_at"]) > current_expiry:
            self._adopt(row, notify=True)
            log.info("Adopted a newer Slack token from the database (expires in %s)",
                     describe(self.seconds_left))

    def start(self) -> None:
        threading.Thread(target=self._run, name="slack-token-refresh", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()

    def _run(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            due_in = self.seconds_left - self.config.token_refresh_margin_s
            if due_in > 0:
                if self._stopping.wait(min(due_in, STORE_POLL_INTERVAL_S)):
                    return
                try:
                    self._adopt_newer_from_store()
                except Exception:
                    log.exception("Could not check the database for a newer Slack token")
                continue

            try:
                self.refresh()
                failures = 0
            except Exception as error:
                failures += 1
                wait = min(30 * 2 ** (failures - 1), 900)
                left = self.seconds_left
                state = (
                    "HAS EXPIRED -- every Slack call is failing until a refresh succeeds"
                    if left <= 0 else f"is still valid for {describe(left)}"
                )
                (log.error if left <= 0 else log.warning)(
                    "Slack token refresh failed (attempt %d): %s Retrying in %ds; the current "
                    "token %s.", failures, error, wait, state,
                )
                if self._stopping.wait(wait):
                    return


def read_stored(config, store) -> Optional[Tuple[str, float]]:
    """(access_token, expires_at) from the database, without refreshing anything.

    For `check`: refreshing there would spend the refresh token the running
    bridge is holding.
    """
    row = store.load_slack_tokens()
    if not row:
        return None
    try:
        access = Fernet(config.token_key.encode()).decrypt(row["access_token"].encode()).decode()
    except InvalidToken:
        raise TokenError("the stored token cannot be decrypted with MMBRIDGE_TOKEN_KEY") from None
    return access, float(row["expires_at"])
