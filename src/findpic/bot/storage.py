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
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

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

-- One row per picture: who sent it, when, what made it, and what findpic
-- concluded. Separate from `events`, which is an interaction log that happened
-- to be wearing a photo log's clothes — four of its columns described a
-- picture and the rest described a button press, and nothing could join them.
--
-- No FOREIGN KEY. `PRAGMA foreign_keys` is off (only journal_mode and
-- synchronous are set), so a constraint here would be decorative, and an
-- ON DELETE CASCADE would make photo retention hostage to event retention —
-- the opposite of what two independent clocks need.
CREATE TABLE IF NOT EXISTS photos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER,          -- NULL when ANALYTICS=0
    -- NOT NULL is load-bearing: the orphan rule in purge_old_events uses
    -- `NOT IN (SELECT user_id FROM photos)`, and NOT IN against a nullable
    -- column evaluates to NULL and silently deletes nothing at all.
    user_id      INTEGER NOT NULL,
    at           TEXT    NOT NULL, -- UTC ISO-8601 seconds, as events.at

    -- how it arrived
    sent_as      TEXT,             -- 'photo' (Telegram re-encoded it) | 'file'
    chat_type    TEXT,
    file_type    TEXT,             -- what exiftool detected
    mime_type    TEXT,             -- what Telegram claimed
    claimed_name TEXT,             -- DISPLAY ONLY. Never used to build a path.
    size_bytes   INTEGER,
    width        INTEGER,
    height       INTEGER,

    -- what made it: read out of the picture, never from Telegram
    make         TEXT,
    model        TEXT,
    os           TEXT,
    lens         TEXT,
    editor       TEXT,

    -- what findpic concluded. Levels, never labels: a label resolves through
    -- the message catalogue, so the same database would read differently
    -- depending on who ran the report — and bot-stats.py cannot import findpic.
    originality  TEXT,             -- good | fair | poor | bad | unknown
    privacy      TEXT,
    structure    TEXT,
    tag_count    INTEGER NOT NULL DEFAULT 0,
    -- A JSON array of stable finding ids. The id is this project's declared
    -- language-neutral contract; the rendered sentence is not.
    finding_ids  TEXT,
    stripped     INTEGER NOT NULL DEFAULT 0,
    had_gps      INTEGER NOT NULL DEFAULT 0,
    has_serial   INTEGER NOT NULL DEFAULT 0,

    -- what the reader did with the report
    clean_offered INTEGER NOT NULL DEFAULT 0,
    tags_taken    INTEGER NOT NULL DEFAULT 0,
    clean_taken   INTEGER NOT NULL DEFAULT 0,
    backup_taken  INTEGER NOT NULL DEFAULT 0,

    -- how the run went
    duration_ms  INTEGER,
    warnings     INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,

    -- When and where the picture was taken. Written only when the operator has
    -- turned capture recording on, or when the archive is keeping the whole
    -- file anyway — at which point refusing a coarse locality in the index
    -- while the exact coordinates sit on disk would be theatre.
    -- Never the exact second, never a street address, never six decimal places.
    taken_date   TEXT,             -- 'YYYY-MM-DD'
    taken_offset TEXT,             -- '+02:00'
    age_days     INTEGER,          -- taken -> sent
    place        TEXT,             -- locality and region, never the street
    country      TEXT,             -- ISO 3166-1 alpha-2
    lat          REAL,             -- 2 dp, about a kilometre
    lon          REAL,

    -- the archived original, when one is kept
    sha256       TEXT,
    bytes_kept   INTEGER,
    state        TEXT,             -- stored|duplicate|skipped_big|skipped_space|error|evicted
    rel_path     TEXT              -- path under the archive root; NULL = not kept
);

CREATE INDEX IF NOT EXISTS photos_at    ON photos (at);
CREATE INDEX IF NOT EXISTS photos_user  ON photos (user_id, at);
CREATE INDEX IF NOT EXISTS photos_event ON photos (event_id);
CREATE INDEX IF NOT EXISTS photos_sha   ON photos (sha256);
"""

#: Bumped whenever SCHEMA gains a column on a table that already ships.
SCHEMA_VERSION = 2

#: The one thing CREATE TABLE IF NOT EXISTS cannot do. A new *table* needs no
#: entry — SCHEMA creates it. Only a column added to a table already on disk.
#:
#: ALTER TABLE has no IF NOT EXISTS in any SQLite version, so a blind ALTER
#: raises "duplicate column name" on the second start and the bot never comes
#: up. Every step is therefore check-then-act.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (("analyses", "photo_id", "INTEGER"),)


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
    #: The picture this report was about, so a button press can be attributed
    #: to it. Without this the ledger can say a warning was shown but never
    #: whether anybody acted on it.
    photo_id: int | None = None


@dataclass
class PhotoRecord:
    """One picture's row, built from a Report rather than from tag names.

    A dataclass rather than a long keyword signature because the column list is
    the thing that has to stay in step with SCHEMA, and here the two sit
    side by side. ``columns()`` skips whatever is None, so a bot with capture
    recording off simply does not write those columns rather than writing NULLs
    that look like "we looked and found nothing".
    """

    user_id: int
    at: str
    event_id: int | None = None
    sent_as: str | None = None
    chat_type: str | None = None
    file_type: str | None = None
    mime_type: str | None = None
    claimed_name: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    make: str | None = None
    model: str | None = None
    os: str | None = None
    lens: str | None = None
    editor: str | None = None
    originality: str | None = None
    privacy: str | None = None
    structure: str | None = None
    tag_count: int = 0
    finding_ids: str | None = None
    stripped: int = 0
    had_gps: int = 0
    has_serial: int = 0
    clean_offered: int = 0
    duration_ms: int | None = None
    warnings: int = 0
    errors: int = 0
    taken_date: str | None = None
    taken_offset: str | None = None
    age_days: int | None = None
    place: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    # What became of the copy, when one was asked for. Set at insert time
    # because the archive runs before the analysis — a file that crashes
    # exiftool is exactly the one worth having on disk.
    sha256: str | None = None
    bytes_kept: int | None = None
    state: str | None = None
    rel_path: str | None = None

    def columns(self) -> list[str]:
        return [name for name, value in self.__dict__.items() if value is not None]

    def values(self) -> list[object]:
        return [value for value in self.__dict__.values() if value is not None]


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
        # Before anything touches the shape of a database that already holds
        # somebody's history. VACUUM INTO cannot run inside a transaction, so it
        # has to come before executescript.
        await self._snapshot_before_upgrade()
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()

    async def _table_columns(self, table: str) -> set[str]:
        # PRAGMA takes no bound parameters, so the name is interpolated. Every
        # name that reaches here is a module constant; none comes from a user.
        async with self.db.execute(f"PRAGMA table_info({table})") as cursor:
            return {row[1] for row in await cursor.fetchall()}

    async def _user_version(self) -> int:
        async with self.db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def _snapshot_before_upgrade(self) -> None:
        """Keep a copy of the old database before changing its shape.

        Only when there is something to lose: a fresh database reports
        ``user_version 0`` exactly as the deployed one does, so the version
        number alone cannot tell them apart — the presence of `events` can.

        ``VACUUM INTO`` rather than a file copy, because it folds the
        write-ahead log in and produces one consistent file from inside this
        connection, with no window where the copy is half a transaction behind.
        Never overwritten: the whole value of a rollback is that it is the
        state before the first upgrade attempt, not before the last one.
        """
        if await self._user_version() >= SCHEMA_VERSION:
            return
        async with self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ) as cursor:
            if await cursor.fetchone() is None:
                return  # a new database has no history to preserve

        backup = self.path.with_name(f"{self.path.name}.v{await self._user_version()}")
        if backup.exists():
            return
        try:
            await self.db.execute(f"VACUUM INTO '{backup}'")
            logger.info("kept a copy of the database before upgrading it: %s", backup)
        except Exception:  # noqa: BLE001 - sqlite < 3.27, or no room for a copy
            logger.warning("could not snapshot the database before upgrading", exc_info=True)

    async def _migrate(self) -> None:
        """Bring an existing database up to the shape SCHEMA describes.

        ``executescript(SCHEMA)`` creates missing tables and does nothing at all
        to an existing one — it does not diff and it does not add columns. So a
        column added to `events` or `analyses` silently would not appear on the
        deployed database, and the next INSERT naming it raises `no such column`
        inside the handler that answers the user: a successful analysis, and no
        report.
        """
        version = await self._user_version()
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            # Written by a newer bot. Warn and carry on: refusing to start would
            # turn a rollback into an outage, and every added column is nullable.
            logger.warning(
                "database is at schema version %s, this build knows %s — "
                "it was written by a newer bot. Continuing.",
                version,
                SCHEMA_VERSION,
            )
            return

        await self.db.execute("BEGIN IMMEDIATE")
        try:
            for table, column, declared in ADDED_COLUMNS:
                if column not in await self._table_columns(table):
                    await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declared}")
                    logger.info("added %s.%s", table, column)
            await self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        if version:
            logger.info("database upgraded from schema %s to %s", version, SCHEMA_VERSION)

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
        self,
        user_id: int,
        file_id: str,
        file_name: str | None,
        now: float,
        photo_id: int | None = None,
    ) -> str:
        token = secrets.token_urlsafe(8)
        await self.db.execute(
            "INSERT INTO analyses (token, user_id, file_id, file_name, photo_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (token, user_id, file_id, file_name, photo_id, now),
        )
        await self.db.commit()
        return token

    async def recall_analysis(self, token: str, user_id: int) -> AnalysisHandle | None:
        """Fetch a handle, scoped to the user who created it.

        The user_id check is the authorisation: a guessed token from another
        chat must not resolve to somebody else's file.
        """
        async with self.db.execute(
            "SELECT token, user_id, file_id, file_name, photo_id FROM analyses"
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
            photo_id=row["photo_id"],
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
        """Attach the camera to the interaction row.

        Superseded by :meth:`record_photo`, which writes a row per picture
        rather than four columns bolted onto a row about a button press. Kept
        because the deployed database has rows in this shape and the report
        still reads them, so the two must agree about what they mean.
        """
        if not event_id:
            return
        await self.db.execute(
            "UPDATE events SET make = ?, model = ?, os = ?, sent_as = ?,"
            " file_type = ?, stripped = ? WHERE id = ?",
            (make, model, os_version, sent_as, file_type, int(stripped), event_id),
        )
        await self.db.commit()

    async def record_photo(self, photo: PhotoRecord) -> int:
        """One row for one picture, and return its id.

        This is what makes "who sent what, from what device, when" answerable.
        Before it, the same facts existed only as columns on the interaction
        log, aggregated into separate counters by the report and never joined
        back together.
        """
        columns = photo.columns()
        placeholders = ", ".join("?" * len(columns))
        cursor = await self.db.execute(
            f"INSERT INTO photos ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(photo.values()),
        )
        await self.db.commit()
        return int(cursor.lastrowid or 0)

    async def note_photo_action(self, photo_id: int | None, action: str) -> None:
        """Record that the reader pressed one of the buttons under a report.

        This is the only thing that closes the funnel: without it the report can
        say a warning was shown but never whether anyone acted on it.
        """
        column = {"tags": "tags_taken", "clean": "clean_taken", "backup": "backup_taken"}.get(
            action
        )
        if not photo_id or column is None:
            return
        await self.db.execute(
            f"UPDATE photos SET {column} = {column} + 1 WHERE id = ?", (photo_id,)
        )
        await self.db.commit()

    async def note_archived(
        self,
        photo_id: int | None,
        *,
        sha256: str | None,
        bytes_kept: int,
        state: str,
        rel_path: str | None,
    ) -> None:
        """Record what became of the copy — including when there is not one.

        A file the archive refused to keep still gets a row saying so. An
        archive whose failures are invisible is worse than no archive: the
        operator would believe photographs were being kept for weeks before
        discovering they were not.
        """
        if not photo_id:
            return
        await self.db.execute(
            "UPDATE photos SET sha256 = ?, bytes_kept = ?, state = ?, rel_path = ? WHERE id = ?",
            (sha256, bytes_kept, state, rel_path, photo_id),
        )
        await self.db.commit()

    async def forget_everything(self, user_id: int) -> tuple[list[str], int]:
        """Delete one person's record, and say what there was to delete.

        Returns the archived files to unlink and how many usage rows went. The
        files are handed back rather than removed here — this class knows about
        SQL and the archive knows about disks, and mixing them is how a delete
        ends up half done in one of the two.

        The `users` row survives, deliberately: it holds a language preference
        the person chose, and silently resetting it would be a worse surprise
        than keeping it. The notice says so rather than claiming "everything".
        """
        async with self.db.execute(
            "SELECT rel_path FROM photos WHERE user_id = ? AND rel_path IS NOT NULL",
            (user_id,),
        ) as cursor:
            files = [row["rel_path"] for row in await cursor.fetchall()]

        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        events = int(row["n"]) if row else 0

        for statement in (
            "DELETE FROM photos WHERE user_id = ?",
            "DELETE FROM events WHERE user_id = ?",
            "DELETE FROM people WHERE user_id = ?",
            "DELETE FROM analyses WHERE user_id = ?",
            "DELETE FROM usage WHERE user_id = ?",
        ):
            await self.db.execute(statement, (user_id,))
        await self.db.commit()
        return files, events

    async def expired_archive_files(self, keep_days: int) -> list[tuple[int, str]]:
        """Kept pictures older than the window, as ``(photo_id, rel_path)``."""
        if keep_days <= 0:
            return []
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).isoformat(
            timespec="seconds"
        )
        async with self.db.execute(
            "SELECT id, rel_path FROM photos WHERE rel_path IS NOT NULL AND at < ?",
            (cutoff,),
        ) as cursor:
            return [(int(row["id"]), row["rel_path"]) for row in await cursor.fetchall()]

    async def forget_archived(self, photo_id: int) -> None:
        """The row survives the file, saying the copy is gone.

        Keeping the row is the point: the ledger still shows the picture was
        received and what was found in it, which is the analytics the operator
        asked for. Only the copy has a shorter life than the record of it.
        """
        await self.db.execute(
            "UPDATE photos SET rel_path = NULL, state = 'evicted' WHERE id = ?", (photo_id,)
        )
        await self.db.commit()

    async def archive_usage(self) -> tuple[int, int]:
        """``(distinct bytes on disk, bytes saved by deduplication)``.

        Summing the column would drift above the truth and start refusing
        writes while the disk was half empty: a duplicate row carries a full
        byte count while costing nothing, and an evicted one costs nothing at
        all. The blobs are what occupy the disk, so the blobs are what count.
        """
        async with self.db.execute(
            "SELECT COALESCE(SUM(bytes), 0) AS held FROM"
            " (SELECT sha256, MAX(bytes_kept) AS bytes FROM photos"
            "   WHERE state = 'stored' AND sha256 IS NOT NULL GROUP BY sha256)"
        ) as cursor:
            row = await cursor.fetchone()
        held = int(row["held"]) if row else 0

        async with self.db.execute(
            "SELECT COALESCE(SUM(bytes_kept), 0) AS total FROM photos"
            " WHERE state IN ('stored', 'duplicate')"
        ) as cursor:
            row = await cursor.fetchone()
        total = int(row["total"]) if row else 0
        return held, max(0, total - held)

    async def user_archive_bytes(self, user_id: int) -> int:
        async with self.db.execute(
            "SELECT COALESCE(SUM(bytes), 0) AS held FROM"
            " (SELECT sha256, MAX(bytes_kept) AS bytes FROM photos"
            "   WHERE state = 'stored' AND user_id = ? AND sha256 IS NOT NULL"
            "   GROUP BY sha256)",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["held"]) if row else 0

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
        await self.db.execute("DELETE FROM photos WHERE at < ?", (cutoff,))
        if removed:
            # Both tables, or a name vanishes from the ledger while that
            # person's pictures are still on file — unattributable rows, which
            # is the exact opposite of what the archive is for. `photos.user_id`
            # is NOT NULL, which is what makes NOT IN safe here.
            await self.db.execute(
                "DELETE FROM people"
                " WHERE user_id NOT IN (SELECT user_id FROM events)"
                "   AND user_id NOT IN (SELECT user_id FROM photos)"
            )
        await self.db.commit()
        return removed
