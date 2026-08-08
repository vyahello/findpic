"""Persistent state: language preference, usage quota, and analysis handles.

SQLite, because the bot must remember a user's language across restarts and a
JSON file would corrupt under concurrent writes. Everything here is small and
bounded — no photo ever touches the database, only a Telegram ``file_id``, which
is a reference Telegram already holds.
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
"""


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


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
