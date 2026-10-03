"""Durable mapping between Slack and Mattermost message IDs.

Two jobs, both essential:

1. **Threading, edits, deletes and reactions.** Every one of those needs to
   name a message that already exists on the other side, which is only possible
   if the pairing was written down when the mirror was created.

2. **Loop prevention.** A message the bridge itself posted comes straight back
   as an event from the receiving platform. Author identity alone is a fragile
   guard -- Slack reports bot posts inconsistently across subtypes, and
   Mattermost webhook-style posts look much like ordinary ones. Checking
   whether the incoming ID is already one half of a known link is exact.

SQLite is a deliberate choice, not a shortcut: the bridge has to run as a single
replica anyway (each platform delivers an event once per connection, so a second
replica would mirror everything twice), which is precisely the workload SQLite
suits. The class is the only thing that touches SQL, so swapping in Postgres
later is a contained change.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

from .models import MATTERMOST, SLACK, MessageLink

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_links (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    slack_channel      TEXT NOT NULL,
    slack_ts           TEXT NOT NULL,
    slack_thread_ts    TEXT,
    mattermost_channel TEXT NOT NULL,
    mattermost_post_id TEXT NOT NULL,
    mattermost_root_id TEXT,
    origin             TEXT NOT NULL,
    created_at         REAL NOT NULL,
    UNIQUE (slack_channel, slack_ts),
    UNIQUE (mattermost_channel, mattermost_post_id)
);

CREATE TABLE IF NOT EXISTS seen_events (
    event_key  TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

-- One row: the current rotating Slack token pair. Values arrive already
-- encrypted; this module never sees a plaintext credential.
CREATE TABLE IF NOT EXISTS slack_tokens (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    access_token          TEXT NOT NULL,
    refresh_token         TEXT NOT NULL,
    expires_at            REAL NOT NULL,
    bootstrap_fingerprint TEXT NOT NULL,
    updated_at            REAL NOT NULL
);
"""


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = self._connect(path)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        """Open the database, creating its directory and explaining any failure.

        sqlite3 reports every one of "no such directory", "not writable" and
        "path is a directory" as the same bare ``unable to open database file``,
        which is not enough to act on.
        """
        directory = os.path.dirname(os.path.abspath(path))
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            raise StoreError(
                f"Cannot create the directory {directory!r} for the mapping database "
                f"({error.strerror}).\n"
                "MMBRIDGE_DB defaults to /data/mmbridge.sqlite3 inside the container. "
                "Running outside Docker, point it at a writable local path, "
                "e.g. MMBRIDGE_DB=mmbridge.sqlite3"
            ) from error

        try:
            return sqlite3.connect(path, check_same_thread=False)
        except sqlite3.OperationalError as error:
            raise StoreError(
                f"Cannot open the mapping database at {path!r} ({error}).\n"
                f"The directory {directory!r} exists but is not writable by this user.\n"
                "The bridge needs this file for threading, edits, deletes and loop "
                "prevention, so it cannot start without it."
            ) from error

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- links ---------------------------------------------------------------

    def save_link(self, link: MessageLink) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT OR REPLACE INTO message_links (
                    slack_channel, slack_ts, slack_thread_ts,
                    mattermost_channel, mattermost_post_id, mattermost_root_id,
                    origin, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link.slack_channel,
                    link.slack_ts,
                    link.slack_thread_ts,
                    link.mattermost_channel,
                    link.mattermost_post_id,
                    link.mattermost_root_id,
                    link.origin,
                    time.time(),
                ),
            )
            self._db.commit()

    def _row_to_link(self, row: Optional[sqlite3.Row]) -> Optional[MessageLink]:
        if row is None:
            return None
        return MessageLink(
            slack_channel=row["slack_channel"],
            slack_ts=row["slack_ts"],
            slack_thread_ts=row["slack_thread_ts"],
            mattermost_channel=row["mattermost_channel"],
            mattermost_post_id=row["mattermost_post_id"],
            mattermost_root_id=row["mattermost_root_id"],
            origin=row["origin"],
        )

    def link_by_slack(self, channel: str, ts: str) -> Optional[MessageLink]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM message_links WHERE slack_channel = ? AND slack_ts = ?",
                (channel, ts),
            ).fetchone()
        return self._row_to_link(row)

    def link_by_mattermost(self, channel: str, post_id: str) -> Optional[MessageLink]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM message_links "
                "WHERE mattermost_channel = ? AND mattermost_post_id = ?",
                (channel, post_id),
            ).fetchone()
        return self._row_to_link(row)

    def link_for(self, platform: str, channel: str, message_id: str) -> Optional[MessageLink]:
        if platform == SLACK:
            return self.link_by_slack(channel, message_id)
        return self.link_by_mattermost(channel, message_id)

    def delete_link(self, link: MessageLink) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM message_links WHERE slack_channel = ? AND slack_ts = ?",
                (link.slack_channel, link.slack_ts),
            )
            self._db.commit()

    # -- rotating Slack token ------------------------------------------------

    def save_slack_tokens(
        self, access_token: str, refresh_token: str, expires_at: float, bootstrap_fingerprint: str,
    ) -> None:
        """Replace the stored token pair in one transaction.

        Atomicity is the whole point: Slack's refresh tokens are single-use, so a
        crash that left the old access token beside the new refresh token -- or
        nothing at all -- would lock the bridge out until the app is reinstalled.
        """
        with self._lock:
            with self._db:
                self._db.execute(
                    """
                    INSERT OR REPLACE INTO slack_tokens (
                        id, access_token, refresh_token, expires_at,
                        bootstrap_fingerprint, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (access_token, refresh_token, expires_at, bootstrap_fingerprint, time.time()),
                )

    def load_slack_tokens(self) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM slack_tokens WHERE id = 1").fetchone()
        return dict(row) if row else None

    # -- event de-duplication ------------------------------------------------

    def mark_seen(self, event_key: str) -> bool:
        """Record `event_key`; return True if this is the first sighting.

        Socket Mode redelivers an event when an acknowledgement is slow, and a
        reconnect can replay one. Without this guard a redelivery posts the
        message a second time.
        """
        if not event_key:
            return True
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO seen_events (event_key, created_at) VALUES (?, ?)",
                (event_key, time.time()),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def prune_seen(self, older_than_s: float = 7 * 24 * 3600) -> int:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM seen_events WHERE created_at < ?", (time.time() - older_than_s,)
            )
            self._db.commit()
            return cursor.rowcount


__all__ = ["Store", "MessageLink", "SLACK", "MATTERMOST"]
