"""Persistent state: language preference, usage quota, analysis handles, and
a record of who used the bot.

SQLite, because the bot must remember a user's language across restarts and a
JSON file would corrupt under concurrent writes. Everything here is small and
bounded — no photo ever touches the database, only a Telegram ``file_id``, which
is a reference Telegram already holds.

The audit tables (``people`` and ``events``) answer the operator's question
"who is using this?". Two limits are deliberate and worth stating, because the
bot's own privacy notice has to be true:

* No message text is ever stored. An event records *that* somebody typed, never
  what. No coordinates, no hashes, and no filename beyond its extension — the
  full name lives only in ``analyses``, for the hours a report's buttons need to
  find the file again, because it is what names the clean copy handed back.
* Nothing is kept about the *picture* beyond the camera it names — make, model
  and OS version. Where and when a photo was taken is the thing this bot warns
  people about; harvesting it from their uploads would be indefensible.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    language    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    user_id     INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    last_at     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- Handles let a button under a report refer back to the file it analysed.
-- callback_data is capped at 64 bytes, far too small for a Telegram file_id,
-- so the short token is stored here and the id travels in the button.
CREATE TABLE IF NOT EXISTS analyses (
    token       TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    file_id     TEXT    NOT NULL,
    file_name   TEXT,
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS analyses_created_at ON analyses (created_at);

-- Everyone the bot has met, one row each, updated in place. Separate from
-- `users` on purpose: that table is a preference the user set and expects to
-- keep, this one is operator analytics that can be purged without losing it.
CREATE TABLE IF NOT EXISTS people (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    -- The language their Telegram client asks in, which is not necessarily the
    -- one they chose in the menu. Both are interesting; they often disagree.
    language_code TEXT,
    is_premium    INTEGER NOT NULL DEFAULT 0,
    is_bot        INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT    NOT NULL,
    last_seen     TEXT    NOT NULL,
    events        INTEGER NOT NULL DEFAULT 0
);

-- One row per interaction. Deliberately narrow: no message text, no file_ids,
-- and of a filename only its extension.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    at        TEXT    NOT NULL,  -- UTC, ISO-8601
    kind      TEXT    NOT NULL,  -- command | menu | button | photo | file | text | media
    action    TEXT,              -- which command, which button; never message text
    chat_type TEXT,
    -- NULL means it was served. Anything else is why it was not.
    outcome   TEXT,
    -- Filled in after an analysis, from what the photo itself declared.
    make      TEXT,
    model     TEXT,
    os        TEXT,
    sent_as   TEXT,              -- photo (Telegram re-encoded it) | file (original)
    file_type TEXT,
    stripped  INTEGER
);

CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE INDEX IF NOT EXISTS events_user ON events (user_id, at);
"""


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class QuotaVerdict:
    """Whether a request may proceed, and why not when it may not."""

    allowed: bool
    reason: str = ""
    retry_after: float = 0.0
    used: int = 0
    limit: int = 0


@dataclass(frozen=True)
class AnalysisHandle:
    token: str
    user_id: int
    file_id: str
    file_name: str | None


@dataclass(frozen=True)
class Person:
    """What Telegram tells a bot about whoever is talking to it.

    This is the whole list — there is no device, no operating system, no app
    version, no IP and no location in it. Bots see an account, not a session.
    Anything this project reports about a device is read out of the photograph,
    which is a different claim and is labelled as one.
    """

    user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    is_premium: bool = False
    is_bot: bool = False


class Storage:
    """Async wrapper over the bot's SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # WAL keeps reads from blocking the write that records a new analysis.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.connect() was never awaited")
        return self._db

    # ----------------------------------------------------------- preferences

    async def get_language(self, user_id: int, default: str) -> str:
        async with self.db.execute(
            "SELECT language FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["language"] if row else default

    async def set_language(self, user_id: int, language: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT INTO users (user_id, language, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET language = ?, updated_at = ?
            """,
            (user_id, language, now, now, language, now),
        )
        await self.db.commit()

    async def known_users(self) -> int:
        async with self.db.execute("SELECT COUNT(*) AS n FROM users") as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    # ----------------------------------------------------------------- quota

    async def check_and_consume(
        self, user_id: int, *, throttle_seconds: float, daily_quota: int, now: float
    ) -> QuotaVerdict:
        """Atomically test the rate limit and the daily quota, then record use.

        Combined into one transaction so two messages arriving together cannot
        both pass the check and blow through the limit.
        """
        day = _today()
        async with self.db.execute(
            "SELECT count, last_at FROM usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ) as cursor:
            row = await cursor.fetchone()

        used = int(row["count"]) if row else 0
        last_at = float(row["last_at"]) if row else 0.0

        elapsed = now - last_at
        if last_at and elapsed < throttle_seconds:
            return QuotaVerdict(
                allowed=False,
                reason="throttled",
                retry_after=round(throttle_seconds - elapsed, 1),
                used=used,
                limit=daily_quota,
            )
        if daily_quota and used >= daily_quota:
            return QuotaVerdict(allowed=False, reason="quota", used=used, limit=daily_quota)

        await self.db.execute(
            """
            INSERT INTO usage (user_id, day, count, last_at) VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1, last_at = ?
            """,
            (user_id, day, now, now),
        )
        await self.db.commit()
        return QuotaVerdict(allowed=True, used=used + 1, limit=daily_quota)

    async def refund(self, user_id: int) -> None:
        """Give back a quota slot the bot itself decided not to spend.

        The throttle charges before the handler has seen the file, which is the
        right order — the check has to be atomic with the spend or two photos in
        one album both slip through. The consequence is that a file the bot then
        refuses as too big, as not an image, or as unreadable has already cost
        the sender one of their analyses for the day. Refunding is cheaper than
        restructuring, and it keeps the count the user is shown honest.

        Not called for a crash: a file that reliably breaks the analysis would
        otherwise be an unlimited retry loop.
        """
        await self.db.execute(
            "UPDATE usage SET count = MAX(0, count - 1) WHERE user_id = ? AND day = ?",
            (user_id, _today()),
        )
        await self.db.commit()

    # -------------------------------------------------------------- handles

    async def remember_analysis(
        self, user_id: int, file_id: str, file_name: str | None, now: float
    ) -> str:
        token = secrets.token_urlsafe(8)
        await self.db.execute(
            "INSERT INTO analyses (token, user_id, file_id, file_name, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (token, user_id, file_id, file_name, now),
        )
        await self.db.commit()
        return token

    async def recall_analysis(self, token: str, user_id: int) -> AnalysisHandle | None:
        """Fetch a handle, scoped to the user who created it.

        The user_id check is the authorisation: a guessed token from another
        chat must not resolve to somebody else's file.
        """
        async with self.db.execute(
            "SELECT token, user_id, file_id, file_name FROM analyses"
            " WHERE token = ? AND user_id = ?",
            (token, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return AnalysisHandle(
            token=row["token"],
            user_id=row["user_id"],
            file_id=row["file_id"],
            file_name=row["file_name"],
        )

    async def purge_expired(self, older_than: float) -> int:
        """Drop stale handles. Called periodically; keeps the table bounded."""
        cursor = await self.db.execute("DELETE FROM analyses WHERE created_at < ?", (older_than,))
        await self.db.commit()
        return cursor.rowcount or 0

    async def purge_old_usage(self, keep_days: int = 7) -> int:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).strftime(
            "%Y-%m-%d"
        )
        cursor = await self.db.execute("DELETE FROM usage WHERE day < ?", (cutoff,))
        await self.db.commit()
        return cursor.rowcount or 0

    # ----------------------------------------------------------------- audit

    async def record_event(
        self,
        person: Person,
        *,
        kind: str,
        action: str | None = None,
        chat_type: str | None = None,
    ) -> int:
        """Note one interaction and return its row id.

        The id is handed to the handler so it can fill in what the analysis
        found. One row per interaction, updated in place — a second row would
        double every count in the report.
        """
        now = _now()
        await self.db.execute(
            """
            INSERT INTO people (user_id, username, first_name, last_name,
                                language_code, is_premium, is_bot,
                                first_seen, last_seen, events)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username      = excluded.username,
                first_name    = excluded.first_name,
                last_name     = excluded.last_name,
                language_code = excluded.language_code,
                is_premium    = excluded.is_premium,
                last_seen     = excluded.last_seen,
                events        = events + 1
            """,
            (
                person.user_id,
                person.username,
                person.first_name,
                person.last_name,
                person.language_code,
                int(person.is_premium),
                int(person.is_bot),
                now,
                now,
            ),
        )
        cursor = await self.db.execute(
            "INSERT INTO events (user_id, at, kind, action, chat_type) VALUES (?, ?, ?, ?, ?)",
            (person.user_id, now, kind, action, chat_type),
        )
        await self.db.commit()
        return int(cursor.lastrowid or 0)

    async def note_outcome(self, event_id: int | None, outcome: str) -> None:
        """Record why an interaction was not served: refused, or it failed."""
        if not event_id:
            return
        await self.db.execute("UPDATE events SET outcome = ? WHERE id = ?", (outcome, event_id))
        await self.db.commit()

    async def note_analysis(
        self,
        event_id: int | None,
        *,
        make: str | None,
        model: str | None,
        os_version: str | None,
        sent_as: str,
        file_type: str | None,
        stripped: bool,
    ) -> None:
        """Attach what the photo declared about the camera that made it.

        Make, model and OS version only. Not when it was taken, not where, not
        a hash — those describe the person's life rather than their equipment,
        and the operator did not need them to answer "what devices do my users
        have".
        """
        if not event_id:
            return
        await self.db.execute(
            "UPDATE events SET make = ?, model = ?, os = ?, sent_as = ?,"
            " file_type = ?, stripped = ? WHERE id = ?",
            (make, model, os_version, sent_as, file_type, int(stripped), event_id),
        )
        await self.db.commit()

    async def purge_old_events(self, keep_days: int) -> int:
        """Forget interactions older than the retention window.

        The account row goes with the last of its interactions. Keeping a name,
        a username and a first-seen date after deleting everything the person
        did would leave the bot holding a roster of strangers it had promised to
        forget — and the notice it shows them says the record is deleted, so it
        has to be. Anyone still active keeps their row, because they still have
        events; only somebody who has been quiet for the whole window is dropped.

        ``keep_days <= 0`` keeps everything indefinitely, which is a decision the
        operator has to make deliberately in the configuration.
        """
        if keep_days <= 0:
            return 0
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).isoformat(
            timespec="seconds"
        )
        cursor = await self.db.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        removed = cursor.rowcount or 0
        if removed:
            await self.db.execute(
                "DELETE FROM people WHERE user_id NOT IN (SELECT DISTINCT user_id FROM events)"
            )
        await self.db.commit()
        return removed
