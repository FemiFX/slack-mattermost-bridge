"""Resolving @names typed for people who only exist on the other platform.

A mention is a link to an account, and in a two-organisation channel the people
on the far side have no account on this platform -- so neither Slack nor
Mattermost can autocomplete them or turn "@anna" into a real mention by itself.
The bridge does it instead, by matching the typed name against the members of
the bridged channel on the other side.

Two rules keep that safe:

- **Only members of the bridged channel are candidates.** Nobody elsewhere in
  either workspace can be pinged through the bridge.
- **Only an unambiguous match counts.** An exact username wins outright;
  otherwise a name must fit exactly one member. Two Annas and "@anna" stays
  plain text -- a missed notification is recoverable, a wrong one is not.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

from .models import BridgeUser

log = logging.getLogger(__name__)

# How long a channel's member list is trusted, and how soon a failed lookup may
# trigger a fresh read -- so someone who just joined can be mentioned without
# every unmatched "@" costing an API call.
MEMBERS_TTL_S = 300
REFRESH_ON_MISS_AFTER_S = 60

_SPLIT = re.compile(r"[\s._\-]+")


def name_keys(user: BridgeUser) -> Set[str]:
    """Every spelling someone might plausibly type after @ for this person.

    "Anna Müller" (handle anna.mueller) yields annamüller, anna.müller, anna,
    müller and anna.mueller. Matching is case-insensitive but otherwise exact:
    "@Mueller" does not find "Müller".
    """
    keys: Set[str] = set()
    for value in (user.username, user.display_name, user.real_name):
        value = (value or "").strip().casefold()
        if not value:
            continue
        keys.add(value.replace(" ", ""))
        parts = [part for part in _SPLIT.split(value) if part]
        if len(parts) > 1:
            keys.add(".".join(parts))
            keys.add(parts[0])
            keys.add(parts[-1])
        elif parts:
            keys.add(parts[0])
    return keys


class NameIndex:
    def __init__(self, users: Iterable[BridgeUser]) -> None:
        self._users: Dict[str, BridgeUser] = {}
        self._by_username: Dict[str, Set[str]] = {}
        self._by_key: Dict[str, Set[str]] = {}
        for user in users:
            self._users[user.user_id] = user
            if user.username:
                self._by_username.setdefault(user.username.casefold(), set()).add(user.user_id)
            for key in name_keys(user):
                self._by_key.setdefault(key, set()).add(user.user_id)

    def lookup(self, name: str) -> Optional[BridgeUser]:
        needle = name.casefold()
        exact = self._by_username.get(needle, set())
        if len(exact) == 1:
            return self._users[next(iter(exact))]
        candidates = self._by_key.get(needle, set())
        if len(candidates) == 1:
            return self._users[next(iter(candidates))]
        return None


class ChannelNames:
    """A NameIndex over one channel's members, rebuilt when it goes stale."""

    def __init__(self, fetch: Callable[[], List[BridgeUser]], label: str = "",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._label = label
        self._clock = clock
        self._lock = threading.Lock()
        self._index: Optional[NameIndex] = None
        self._built_at = float("-inf")

    def _rebuild(self) -> None:
        try:
            index = NameIndex(self._fetch())
        except Exception:
            log.warning("Could not read the members of %s for mention matching", self._label,
                        exc_info=True)
            return
        self._index, self._built_at = index, self._clock()

    def lookup(self, name: str) -> Optional[BridgeUser]:
        with self._lock:
            age = self._clock() - self._built_at
            if self._index is None or age > MEMBERS_TTL_S:
                self._rebuild()
            found = self._index.lookup(name) if self._index else None
            if found is None and self._clock() - self._built_at > REFRESH_ON_MISS_AFTER_S:
                self._rebuild()
                found = self._index.lookup(name) if self._index else None
            return found
