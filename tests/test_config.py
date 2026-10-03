import pytest

from mmbridge.config import ChannelPair, Config, ConfigError, load_env_file


def test_a_pair_parses_with_and_without_a_label():
    assert ChannelPair.parse("C1:M1").label == ""
    assert ChannelPair.parse("C1:M1:project").label == "project"


@pytest.mark.parametrize("raw", ["C1", "C1:M1:x:y", ":M1", "C1:"])
def test_a_malformed_pair_is_rejected(raw):
    with pytest.raises(ConfigError):
        ChannelPair.parse(raw)


def _env(monkeypatch, **overrides):
    base = {
        "SLACK_BOT_TOKEN": "xoxb-x", "SLACK_APP_TOKEN": "xapp-x",
        "MATTERMOST_URL": "https://mm.test/", "MATTERMOST_TOKEN": "t",
        "MMBRIDGE_PAIRS": "C1:M1",
    }
    base.update(overrides)
    for key in list(base) + ["MMBRIDGE_DIRECTION", "MMBRIDGE_SYNC_FILES"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


def test_config_loads_from_the_environment(monkeypatch):
    _env(monkeypatch)
    config = Config.from_env()
    assert config.mattermost_url == "https://mm.test"  # trailing slash trimmed
    assert config.pairs[0].slack_channel_id == "C1"
    assert config.slack_to_mm and config.mm_to_slack


def test_a_missing_credential_is_reported_by_name(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("MATTERMOST_TOKEN")
    with pytest.raises(ConfigError, match="MATTERMOST_TOKEN"):
        Config.from_env()


def test_an_empty_allowlist_is_rejected(monkeypatch):
    _env(monkeypatch, MMBRIDGE_PAIRS="")
    with pytest.raises(ConfigError, match="MMBRIDGE_PAIRS"):
        Config.from_env()


def test_a_channel_may_not_appear_in_two_pairs(monkeypatch):
    """Two pairs sharing a channel would echo every message between them."""
    _env(monkeypatch, MMBRIDGE_PAIRS="C1:M1,C1:M2")
    with pytest.raises(ConfigError, match="more than one pair"):
        Config.from_env()


def test_a_bot_token_in_the_app_token_slot_is_caught(monkeypatch):
    _env(monkeypatch, SLACK_APP_TOKEN="xoxb-wrong")
    with pytest.raises(ConfigError, match="xapp-"):
        Config.from_env()


def test_an_unknown_direction_is_rejected(monkeypatch):
    _env(monkeypatch, MMBRIDGE_DIRECTION="sideways")
    with pytest.raises(ConfigError, match="not valid"):
        Config.from_env()


def test_directions_gate_the_right_way(monkeypatch):
    _env(monkeypatch, MMBRIDGE_DIRECTION="slack_to_mm")
    config = Config.from_env()
    assert config.slack_to_mm and not config.mm_to_slack


def test_pair_lookup_works_from_both_sides(monkeypatch):
    _env(monkeypatch, MMBRIDGE_PAIRS="C1:M1,C2:M2")
    config = Config.from_env()
    assert config.pair_for_slack("C2").mattermost_channel_id == "M2"
    assert config.pair_for_mattermost("M1").slack_channel_id == "C1"
    assert config.pair_for_slack("nope") is None


def test_an_env_file_seeds_but_never_overrides(monkeypatch, tmp_path):
    env_file = tmp_path / ".env.test"
    env_file.write_text('FROM_FILE="yes"\nALREADY_SET=file\n# comment\n\n')
    monkeypatch.setenv("ALREADY_SET", "environment")
    load_env_file(str(env_file))
    import os
    assert os.environ["FROM_FILE"] == "yes"
    assert os.environ["ALREADY_SET"] == "environment"


# -- token rotation ---------------------------------------------------------------

def _rotation_env(monkeypatch, **overrides):
    from mmbridge.slack_tokens import generate_key
    for key in ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_CLIENT_SECRET_FILE",
                "MMBRIDGE_TOKEN_KEY", "MMBRIDGE_TOKEN_KEY_FILE", "SLACK_REFRESH_TOKEN",
                "MMBRIDGE_TOKEN_REFRESH_MARGIN", "SLACK_BOT_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    values = {"SLACK_CLIENT_ID": "123.456", "SLACK_CLIENT_SECRET": "shh",
              "MMBRIDGE_TOKEN_KEY": generate_key()}
    values.update(overrides)
    _env(monkeypatch)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_rotation_is_on_when_both_client_credentials_are_set(monkeypatch):
    _rotation_env(monkeypatch)
    assert Config.from_env().slack_token_rotation


def test_half_a_set_of_client_credentials_is_rejected(monkeypatch):
    _rotation_env(monkeypatch, SLACK_CLIENT_SECRET=None)
    with pytest.raises(ConfigError, match="both SLACK_CLIENT_ID and SLACK_CLIENT_SECRET"):
        Config.from_env()


def test_rotation_requires_an_encryption_key(monkeypatch):
    _rotation_env(monkeypatch, MMBRIDGE_TOKEN_KEY=None)
    with pytest.raises(ConfigError, match="genkey"):
        Config.from_env()


def test_a_malformed_encryption_key_is_rejected(monkeypatch):
    _rotation_env(monkeypatch, MMBRIDGE_TOKEN_KEY="not-a-key")
    with pytest.raises(ConfigError, match="not usable"):
        Config.from_env()


def test_with_rotation_the_bot_token_is_optional(monkeypatch):
    """It is only needed once, to bootstrap, and should then be removed."""
    _rotation_env(monkeypatch)
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    Config.from_env()


def test_a_rotating_token_without_rotation_configured_is_caught(monkeypatch):
    _env(monkeypatch, SLACK_BOT_TOKEN="xoxe.xoxb-1-abc")
    for key in ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigError, match="expires after 12 hours"):
        Config.from_env()


def test_secrets_can_be_read_from_files(monkeypatch, tmp_path):
    """Docker and Swarm secrets arrive as files under /run/secrets."""
    secret = tmp_path / "client_secret"
    secret.write_text("from-a-file\n")
    _rotation_env(monkeypatch, SLACK_CLIENT_SECRET=None, SLACK_CLIENT_SECRET_FILE=str(secret))
    assert Config.from_env().slack_client_secret == "from-a-file"


def test_an_unreadable_secret_file_is_reported(monkeypatch, tmp_path):
    _rotation_env(monkeypatch, SLACK_CLIENT_SECRET=None,
                  SLACK_CLIENT_SECRET_FILE=str(tmp_path / "missing"))
    with pytest.raises(ConfigError, match="cannot be read"):
        Config.from_env()


@pytest.mark.parametrize("margin", ["30", "43200"])
def test_the_refresh_margin_must_leave_room_inside_the_token_lifetime(monkeypatch, margin):
    _rotation_env(monkeypatch, MMBRIDGE_TOKEN_REFRESH_MARGIN=margin)
    with pytest.raises(ConfigError, match="between 60 and 43140"):
        Config.from_env()
