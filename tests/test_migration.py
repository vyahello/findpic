"""Tests for upgrading a database that already holds somebody's history.

The bot's schema is created with ``CREATE TABLE IF NOT EXISTS``, which does
nothing whatever to a table that already exists — it does not diff, and it does
not add columns. So a column added to ``events`` or ``analyses`` silently would
not appear on the deployed database, and the next INSERT naming it raises
``no such column`` inside the handler that answers the user: a successful
analysis, and no report.

The important test here is the last one. It compares the *shape* an upgraded
database ends up with against the shape a fresh one is created with, so it fails
the day somebody adds a column to SCHEMA and forgets ADDED_COLUMNS — which is
the whole failure mode this machinery exists to prevent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from findpic.bot.storage import SCHEMA_VERSION, Person, Storage

#: A frozen copy of the schema as it shipped, before `photos` and
#: `analyses.photo_id` existed. Frozen on purpose: pointing this at the live
#: SCHEMA would make the test pass by construction and prove nothing.
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, language TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL, day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, last_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day));
CREATE TABLE IF NOT EXISTS analyses (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, file_id TEXT NOT NULL,
    file_name TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS people (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
    language_code TEXT, is_premium INTEGER NOT NULL DEFAULT 0,
    is_bot INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL, events INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    at TEXT NOT NULL, kind TEXT NOT NULL, action TEXT, chat_type TEXT,
    outcome TEXT, make TEXT, model TEXT, os TEXT, sent_as TEXT,
    file_type TEXT, stripped INTEGER);
"""

TABLES = ("users", "usage", "analyses", "people", "events", "photos")


def deployed(path: Path, rows: int = 1) -> Path:
    """A database in the shape the VPS is actually running."""
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    for index in range(rows):
        conn.execute(
            "INSERT INTO events (user_id, at, kind, make, model)"
            " VALUES (?, ?, 'file', 'Apple', 'iPhone X')",
            (7, f"2026-08-0{index + 1}T10:00:00+00:00"),
        )
    conn.execute(
        "INSERT INTO analyses (token, user_id, file_id, file_name, created_at)"
        " VALUES ('tok', 7, 'FILE', 'a.jpg', 1000.0)"
    )
    conn.commit()
    conn.close()
    return path


async def shape(storage: Storage, table: str) -> list[tuple[str, str]]:
    """Column names *and* declared types.

    Names alone would pass even when SCHEMA and ADDED_COLUMNS disagree about a
    column's type, which catches only half the drift.
    """
    async with storage.db.execute(f"PRAGMA table_info({table})") as cursor:
        return sorted((row[1], row[2].upper()) for row in await cursor.fetchall())


async def version(storage: Storage) -> int:
    async with storage.db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return int(row[0])


async def test_an_upgraded_database_matches_a_fresh_one(tmp_path: Path) -> None:
    """The test that keeps this maintainable rather than a one-off patch."""
    old = Storage(deployed(tmp_path / "old.sqlite3"))
    fresh = Storage(tmp_path / "new.sqlite3")
    await old.connect()
    await fresh.connect()
    try:
        for table in TABLES:
            assert await shape(old, table) == await shape(fresh, table), table
        assert await version(old) == SCHEMA_VERSION
        assert await version(fresh) == SCHEMA_VERSION
    finally:
        await old.close()
        await fresh.close()


async def test_the_history_survives_the_upgrade(tmp_path: Path) -> None:
    storage = Storage(deployed(tmp_path / "bot.sqlite3", rows=3))
    await storage.connect()
    try:
        async with storage.db.execute("SELECT COUNT(*) AS n FROM events") as cursor:
            assert (await cursor.fetchone())["n"] == 3
        async with storage.db.execute("SELECT * FROM analyses") as cursor:
            row = await cursor.fetchone()
        assert row["file_name"] == "a.jpg"
        assert row["photo_id"] is None
    finally:
        await storage.close()


async def test_the_old_database_is_kept_before_it_is_changed(tmp_path: Path) -> None:
    """The rollback. Nothing else can undo an upgrade in place."""
    path = deployed(tmp_path / "bot.sqlite3")
    storage = Storage(path)
    await storage.connect()
    await storage.close()

    backup = path.with_name(path.name + ".v0")
    assert backup.exists(), "no copy was kept before the schema changed"

    conn = sqlite3.connect(backup)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(analyses)")}
    assert "photo_id" not in columns, "the copy is of the new shape, not the old one"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()


async def test_a_fresh_database_leaves_no_snapshot(tmp_path: Path) -> None:
    """There is nothing to roll back to, so a copy would only be litter."""
    path = tmp_path / "bot.sqlite3"
    storage = Storage(path)
    await storage.connect()
    await storage.close()
    assert not list(tmp_path.glob("*.v*"))


async def test_upgrading_twice_is_a_no_op(tmp_path: Path) -> None:
    """ALTER TABLE has no IF NOT EXISTS, so a blind retry never starts."""
    path = deployed(tmp_path / "bot.sqlite3")
    for _ in range(3):
        storage = Storage(path)
        await storage.connect()
        assert await version(storage) == SCHEMA_VERSION
        await storage.close()

    conn = sqlite3.connect(path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(analyses)")]
    assert columns.count("photo_id") == 1
    conn.close()


async def test_a_newer_database_is_left_alone(tmp_path: Path) -> None:
    """Written by a bot ahead of this one. Refusing to start would turn a
    rollback into an outage, and every added column is nullable."""
    path = deployed(tmp_path / "bot.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    storage = Storage(path)
    await storage.connect()
    try:
        assert await version(storage) == 99
    finally:
        await storage.close()


async def test_the_upgraded_database_can_be_written_to(tmp_path: Path) -> None:
    """The failure this exists to prevent: the column is missing and the INSERT
    raises inside the handler that was about to answer the user."""
    storage = Storage(deployed(tmp_path / "bot.sqlite3"))
    await storage.connect()
    try:
        event_id = await storage.record_event(Person(7, "someone"), kind="file")
        token = await storage.remember_analysis(7, "FILE", "b.jpg", now=1.0, photo_id=5)
        handle = await storage.recall_analysis(token, 7)
        assert handle is not None and handle.photo_id == 5
        assert event_id
    finally:
        await storage.close()
