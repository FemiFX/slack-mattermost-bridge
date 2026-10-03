"""Telling a picture someone chose apart from Slack's generated default."""
import time
from urllib.parse import parse_qs, urlparse

from mmbridge.slack_adapter import (
    USER_CACHE_TTL_S, SlackAdapter, gravatar_probe_url, has_own_avatar,
)

GRAVATAR = ("https://secure.gravatar.com/avatar/9528e94b.jpg?s=192"
            "&d=https%3A%2F%2Fa.slack-edge.com%2Fdf10d%2Fimg%2Favatars%2Fava_0001-192.png")
UPLOADED = "https://avatars.slack-edge.com/2026-09-29/123_abc_192.png"
DEFAULT = "https://a.slack-edge.com/df10d/img/avatars/ava_0001-192.png"


def never(url):
    raise AssertionError("no probe should be needed")


def test_a_picture_uploaded_to_slack_counts_without_asking_anyone():
    assert has_own_avatar({"is_custom_image": True, "image_192": UPLOADED}, probe=never)


def test_a_real_gravatar_counts():
    assert has_own_avatar({"image_192": GRAVATAR}, probe=lambda url: True)


def test_a_gravatar_address_with_nothing_behind_it_does_not():
    assert not has_own_avatar({"image_192": GRAVATAR}, probe=lambda url: False)


def test_slacks_generated_default_does_not():
    assert not has_own_avatar({"image_192": DEFAULT}, probe=lambda url: url.endswith("never"))


def test_no_picture_at_all_does_not():
    assert not has_own_avatar({}, probe=never)


def test_the_probe_asks_gravatar_for_404_instead_of_slacks_default():
    query = parse_qs(urlparse(gravatar_probe_url(GRAVATAR)).query)
    assert query["d"] == ["404"]
    assert query["s"] == ["192"]


def test_only_gravatar_addresses_are_probed():
    assert gravatar_probe_url(UPLOADED) is None
    assert gravatar_probe_url(DEFAULT) is None


# -- the profile cache ------------------------------------------------------------

class CountingClient:
    def __init__(self):
        self.calls = 0

    def users_info(self, user):
        self.calls += 1
        return {"user": {"id": user, "name": "anna",
                         "profile": {"display_name": "Anna", "is_custom_image": True,
                                     "image_192": UPLOADED}}}


def _adapter():
    adapter = SlackAdapter.__new__(SlackAdapter)   # skip auth.test
    adapter.client = CountingClient()
    adapter._users = {}
    import threading
    adapter._users_lock = threading.Lock()
    return adapter


def test_a_profile_is_cached_within_the_ttl():
    adapter = _adapter()
    adapter.user("U1"); adapter.user("U1")
    assert adapter.client.calls == 1


def test_a_stale_profile_is_read_again_so_a_new_picture_shows_up(monkeypatch):
    adapter = _adapter()
    adapter.user("U1")
    later = time.monotonic() + USER_CACHE_TTL_S + 1
    monkeypatch.setattr("mmbridge.slack_adapter.time.monotonic", lambda: later)
    adapter.user("U1")
    assert adapter.client.calls == 2


def test_name_lookup_still_works_with_the_timestamped_cache():
    adapter = _adapter()
    adapter.user("U1")
    assert adapter.find_user_id_by_name("anna") == "U1"
