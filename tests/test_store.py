import pytest

from mmbridge.models import MATTERMOST, SLACK, MessageLink
from mmbridge.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "test.sqlite3"))
    yield instance
    instance.close()


def link(ts="111.1", post_id="p1", origin=SLACK, **kwargs):
    return MessageLink(
        slack_channel="C1", slack_ts=ts,
        mattermost_channel="M1", mattermost_post_id=post_id,
        origin=origin, **kwargs,
    )


def test_a_link_is_findable_from_either_side(store):
    store.save_link(link())
    assert store.link_by_slack("C1", "111.1").mattermost_post_id == "p1"
    assert store.link_by_mattermost("M1", "p1").slack_ts == "111.1"


def test_link_for_dispatches_on_platform(store):
    store.save_link(link())
    assert store.link_for(SLACK, "C1", "111.1") is not None
    assert store.link_for(MATTERMOST, "M1", "p1") is not None


def test_counterpart_returns_the_other_sides_id(store):
    saved = link()
    assert saved.counterpart(MATTERMOST) == "p1"
    assert saved.counterpart(SLACK) == "111.1"


def test_an_unknown_message_has_no_link(store):
    assert store.link_by_slack("C1", "999.9") is None
    assert store.link_by_mattermost("M1", "nope") is None


def test_saving_the_same_message_twice_does_not_duplicate(store):
    store.save_link(link(post_id="p1"))
    store.save_link(link(post_id="p2"))
    # The Slack timestamp is unique, so the second save replaces the first
    # rather than leaving two rows that disagree about the counterpart.
    assert store.link_by_slack("C1", "111.1").mattermost_post_id == "p2"
    assert store.link_by_mattermost("M1", "p1") is None


def test_deleting_a_link_removes_both_directions(store):
    saved = link()
    store.save_link(saved)
    store.delete_link(saved)
    assert store.link_by_slack("C1", "111.1") is None
    assert store.link_by_mattermost("M1", "p1") is None


def test_an_event_is_only_new_once(store):
    assert store.mark_seen("evt-1") is True
    assert store.mark_seen("evt-1") is False


def test_an_empty_event_key_is_always_treated_as_new(store):
    assert store.mark_seen("") is True
    assert store.mark_seen("") is True


def test_pruning_clears_old_events_only(store):
    store.mark_seen("old")
    assert store.prune_seen(older_than_s=-1) == 1
    assert store.mark_seen("old") is True


def test_the_mapping_survives_reopening(store, tmp_path):
    store.save_link(link())
    store.close()
    reopened = Store(str(tmp_path / "test.sqlite3"))
    assert reopened.link_by_slack("C1", "111.1") is not None
    reopened.close()


def test_a_missing_parent_directory_is_created(tmp_path):
    """A nested path should just work rather than failing to open."""
    store = Store(str(tmp_path / "nested" / "dir" / "bridge.sqlite3"))
    assert store.mark_seen("probe") is True
    store.close()


def test_an_unwritable_path_explains_the_docker_default(tmp_path):
    """The /data default is container-only; running locally must say so."""
    from mmbridge.store import StoreError

    with pytest.raises(StoreError, match="MMBRIDGE_DB"):
        Store("/proc/definitely-not-writable/bridge.sqlite3")
