"""Matching a typed @name to exactly one member of the bridged channel."""
from mmbridge.mentions import MEMBERS_TTL_S, REFRESH_ON_MISS_AFTER_S, ChannelNames, NameIndex, name_keys
from mmbridge.models import SLACK, BridgeUser


def person(uid, username="", display="", real=""):
    return BridgeUser(platform=SLACK, user_id=uid, display_name=display, username=username, real_name=real)


ANNA = person("U1", "anna.mueller", "Anna", "Anna Müller")
ANNA_K = person("U2", "anna.k", "Anna K", "Anna Klein")
BEN = person("U3", "ben", "Ben", "Ben Schulz")


def test_the_spellings_someone_might_type():
    assert {"anna.mueller", "anna", "müller", "annamüller", "anna.müller"} <= name_keys(ANNA)


def test_a_unique_first_name_matches():
    assert NameIndex([ANNA, BEN]).lookup("anna") is ANNA


def test_an_ambiguous_first_name_matches_nobody():
    """Two Annas: a missed notification is recoverable, a wrong one is not."""
    assert NameIndex([ANNA, ANNA_K, BEN]).lookup("anna") is None


def test_an_exact_username_wins_even_when_the_name_is_ambiguous():
    assert NameIndex([ANNA, ANNA_K]).lookup("anna.k") is ANNA_K


def test_full_name_last_name_and_case_all_work():
    index = NameIndex([ANNA, ANNA_K, BEN])
    assert index.lookup("AnnaMüller") is ANNA
    assert index.lookup("Klein") is ANNA_K
    assert index.lookup("BEN") is BEN


def test_an_unknown_name_matches_nobody():
    assert NameIndex([ANNA]).lookup("carla") is None


# -- the cached member list ---------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_members_are_read_once_and_reused(tmp_path):
    calls, clock = [], Clock()
    names = ChannelNames(lambda: calls.append(1) or [ANNA], clock=clock)
    names.lookup("anna"); names.lookup("anna")
    assert len(calls) == 1


def test_a_miss_rereads_the_members_so_a_newcomer_can_be_mentioned():
    members, clock = [ANNA], Clock()
    names = ChannelNames(lambda: list(members), clock=clock)
    assert names.lookup("ben") is None
    members.append(BEN)
    clock.now += REFRESH_ON_MISS_AFTER_S + 1
    assert names.lookup("ben") is BEN


def test_a_miss_does_not_reread_straight_away():
    calls, clock = [], Clock()
    names = ChannelNames(lambda: calls.append(1) or [ANNA], clock=clock)
    names.lookup("nobody"); names.lookup("nobody")
    assert len(calls) == 1


def test_the_member_list_expires():
    calls, clock = [], Clock()
    names = ChannelNames(lambda: calls.append(1) or [ANNA], clock=clock)
    names.lookup("anna")
    clock.now += MEMBERS_TTL_S + 1
    names.lookup("anna")
    assert len(calls) == 2


def test_an_unreadable_member_list_means_no_match_not_a_crash():
    def broken():
        raise RuntimeError("down")
    assert ChannelNames(broken).lookup("anna") is None
