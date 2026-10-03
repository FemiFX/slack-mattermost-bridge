"""Token rotation: bootstrap, encrypted persistence, refresh ordering."""
import logging

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from mmbridge.config import ChannelPair, Config
from mmbridge.slack_tokens import (
    SlackOAuth, SlackTokenManager, TokenError, fingerprint, generate_key, read_stored,
)
from mmbridge.store import Store

KEY = generate_key()
TWELVE_HOURS = 43200


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeOAuth:
    """Slack's OAuth endpoints: every refresh token is single-use."""

    def __init__(self):
        self.issued = 0
        self.valid_refresh = set()
        self.calls = []
        self.legacy_still_works = False
        self.fail_with = None

    def _pair(self):
        self.issued += 1
        refresh = f"xoxe-1-refresh-{self.issued}"
        self.valid_refresh.add(refresh)
        return {"ok": True, "access_token": f"xoxe.xoxb-access-{self.issued}",
                "refresh_token": refresh, "expires_in": TWELVE_HOURS, "token_type": "bot"}

    def _error(self, code):
        return SlackApiError(code, SlackResponse(
            client=None, http_verb="POST", api_url="", req_args={},
            data={"ok": False, "error": code}, headers={}, status_code=200))

    def exchange(self, legacy_token):
        self.calls.append(("exchange", legacy_token))
        if self.fail_with:
            raise self._error(self.fail_with)
        return self._pair()

    def refresh(self, refresh_token):
        self.calls.append(("refresh", refresh_token))
        if self.fail_with:
            raise self._error(self.fail_with)
        if refresh_token not in self.valid_refresh:
            raise self._error("invalid_refresh_token")
        self.valid_refresh.discard(refresh_token)
        return self._pair()

    def token_works(self, token):
        return self.legacy_still_works


def make_config(tmp_path, **overrides):
    values = dict(
        slack_bot_token="xoxb-legacy", slack_app_token="xapp-x",
        mattermost_url="https://mm.test", mattermost_token="t",
        pairs=[ChannelPair("C1", "M1")], db_path=str(tmp_path / "db.sqlite3"),
        slack_client_id="123.456", slack_client_secret="shh", token_key=KEY,
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def env(tmp_path):
    config = make_config(tmp_path)
    store = Store(config.db_path)
    oauth, clock = FakeOAuth(), Clock()
    yield config, store, oauth, clock
    store.close()


def manager(config, store, oauth, clock):
    return SlackTokenManager(config, store, oauth=oauth, clock=clock)


# -- bootstrap ------------------------------------------------------------------

def test_a_long_lived_token_is_exchanged_on_first_start(env):
    config, store, oauth, clock = env
    token = manager(config, store, oauth, clock).ensure()
    assert oauth.calls == [("exchange", "xoxb-legacy")]
    assert token == "xoxe.xoxb-access-1"


def test_a_refresh_token_bootstraps_an_app_installed_after_opting_in(tmp_path):
    config = make_config(tmp_path, slack_bot_token="", slack_refresh_token="xoxe-1-refresh-0")
    store, oauth = Store(config.db_path), FakeOAuth()
    oauth.valid_refresh.add("xoxe-1-refresh-0")
    try:
        assert manager(config, store, oauth, Clock()).ensure() == "xoxe.xoxb-access-1"
        assert oauth.calls == [("refresh", "xoxe-1-refresh-0")]
    finally:
        store.close()


def test_a_rotating_bot_token_without_its_refresh_token_is_refused(tmp_path):
    config = make_config(tmp_path, slack_bot_token="xoxe.xoxb-already-rotating")
    store = Store(config.db_path)
    try:
        with pytest.raises(TokenError, match="SLACK_REFRESH_TOKEN"):
            manager(config, store, FakeOAuth(), Clock()).ensure()
    finally:
        store.close()


def test_nothing_stored_and_nothing_to_bootstrap_from_is_explained(tmp_path):
    config = make_config(tmp_path, slack_bot_token="")
    store = Store(config.db_path)
    try:
        with pytest.raises(TokenError, match="nothing to bootstrap from"):
            manager(config, store, FakeOAuth(), Clock()).ensure()
    finally:
        store.close()


def test_a_bootstrap_rejected_by_slack_gives_the_reason(env):
    config, store, oauth, clock = env
    oauth.fail_with = "token_rotation_not_enabled"
    with pytest.raises(TokenError, match="Advanced token security"):
        manager(config, store, oauth, clock).ensure()


def test_a_legacy_token_that_survives_the_exchange_is_flagged(env, caplog):
    config, store, oauth, clock = env
    oauth.legacy_still_works = True
    with caplog.at_level(logging.WARNING):
        manager(config, store, oauth, clock).ensure()
    assert "still works after the exchange" in caplog.text


# -- persistence ----------------------------------------------------------------

def test_the_database_never_holds_a_plaintext_token(env):
    config, store, oauth, clock = env
    manager(config, store, oauth, clock).ensure()
    row = store.load_slack_tokens()
    assert "xoxe" not in row["access_token"] and "xoxe" not in row["refresh_token"]
    assert "xoxb-legacy" not in row["bootstrap_fingerprint"]


def test_a_restart_uses_the_stored_token_without_calling_slack(env):
    config, store, oauth, clock = env
    manager(config, store, oauth, clock).ensure()
    oauth.calls.clear()
    assert manager(config, store, oauth, clock).ensure() == "xoxe.xoxb-access-1"
    assert oauth.calls == []


def test_the_bootstrap_token_can_be_removed_once_used(env, tmp_path):
    config, store, oauth, clock = env
    manager(config, store, oauth, clock).ensure()
    config.slack_bot_token = ""
    assert manager(config, store, oauth, clock).ensure() == "xoxe.xoxb-access-1"


def test_new_bootstrap_values_are_used_when_supplied(env):
    """A changed SLACK_BOT_TOKEN means someone reinstalled and wants it used."""
    config, store, oauth, clock = env
    manager(config, store, oauth, clock).ensure()
    config.slack_bot_token = "xoxb-after-reinstall"
    manager(config, store, oauth, clock).ensure()
    assert oauth.calls[-1] == ("exchange", "xoxb-after-reinstall")
    assert store.load_slack_tokens()["bootstrap_fingerprint"] == fingerprint("xoxb-after-reinstall", "")


def test_the_wrong_key_is_reported_not_crashed_on(env):
    config, store, oauth, clock = env
    manager(config, store, oauth, clock).ensure()
    config.token_key = generate_key()
    with pytest.raises(TokenError, match="cannot be decrypted"):
        manager(config, store, oauth, clock).ensure()


def test_read_stored_does_not_refresh(env):
    """`check` uses it -- refreshing there would spend the bridge's refresh token."""
    config, store, oauth, clock = env
    manager(config, store, oauth, clock).ensure()
    oauth.calls.clear()
    access, _ = read_stored(config, store)
    assert access == "xoxe.xoxb-access-1"
    assert oauth.calls == []


# -- refresh ----------------------------------------------------------------------

def test_a_token_inside_the_margin_is_refreshed_on_start(env):
    config, store, oauth, clock = env
    manager(config, store, oauth, clock).ensure()
    clock.now += TWELVE_HOURS - config.token_refresh_margin_s + 1
    assert manager(config, store, oauth, clock).ensure() == "xoxe.xoxb-access-2"


def test_the_new_pair_is_saved_before_the_bridge_switches_to_it(env):
    """If the process died right after switching, the pair must already be on disk."""
    config, store, oauth, clock = env
    m = manager(config, store, oauth, clock)
    m.ensure()

    seen = []
    m.on_refresh = lambda token: seen.append(read_stored(config, store)[0] == token)
    m.refresh(force=True)
    assert seen == [True]


def test_a_refresh_by_another_process_is_adopted_not_repeated(env):
    """`mmbridge refresh-token` spent our refresh token; using it again would fail."""
    config, store, oauth, clock = env
    bridge = manager(config, store, oauth, clock)
    bridge.ensure()

    clock.now += 60
    manager(config, store, oauth, clock).refresh(force=True)     # the other process
    oauth.calls.clear()

    switched = []
    bridge.on_refresh = switched.append
    bridge.refresh()
    assert oauth.calls == [], "the spent refresh token was used again"
    assert switched == ["xoxe.xoxb-access-2"]


def test_a_failed_refresh_leaves_the_stored_pair_intact(env):
    config, store, oauth, clock = env
    m = manager(config, store, oauth, clock)
    m.ensure()
    before = store.load_slack_tokens()
    oauth.fail_with = "bad_client_secret"
    with pytest.raises(TokenError, match="regenerated"):
        m.refresh(force=True)
    assert store.load_slack_tokens() == before
    assert m.access_token == "xoxe.xoxb-access-1"


def test_a_response_without_a_token_pair_names_fields_but_not_values(env):
    config, store, oauth, clock = env
    oauth.exchange = lambda token: {"ok": True, "access_token": "xoxb-SECRET-VALUE"}
    with pytest.raises(TokenError) as raised:
        manager(config, store, oauth, clock).ensure()
    assert "access_token" in str(raised.value)
    assert "SECRET-VALUE" not in str(raised.value)


def test_secrets_travel_in_the_request_body_not_the_url(monkeypatch):
    calls = []

    def fake_api_call(self, method, **kwargs):
        calls.append((method, kwargs))
        return SlackResponse(client=None, http_verb="POST", api_url="", req_args={},
                             data={"ok": True}, headers={}, status_code=200)

    monkeypatch.setattr("slack_sdk.WebClient.api_call", fake_api_call)
    oauth = SlackOAuth("123.456", "shh")
    oauth.exchange("xoxb-legacy")
    oauth.refresh("xoxe-1-r")
    for method, kwargs in calls:
        assert "params" not in kwargs, f"{method} would put secrets in the URL"
    assert calls[0][1]["data"]["client_secret"] == "shh"
    assert calls[1][1]["data"]["refresh_token"] == "xoxe-1-r"


# -- bridge integration ----------------------------------------------------------

def test_a_refresh_switches_the_live_slack_client(tmp_path):
    from conftest import FakeMattermost, FakeSlack
    from mmbridge.bridge import Bridge

    class ClientHolder(FakeSlack):
        class client:
            token = "old"

    bridge = Bridge(make_config(tmp_path), slack=ClientHolder(), mattermost=FakeMattermost())
    try:
        bridge._switch_slack_token("xoxe.xoxb-new")
        assert bridge.slack.client.token == "xoxe.xoxb-new"
    finally:
        bridge.store.close()
