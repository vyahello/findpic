"""Tests for scripts/bot-stats.py, the usage report.

The script is deliberately standalone — standard library only, so it runs on a
server with nothing installed — which is why it is loaded by path here rather
than imported. Its database is built through the bot's own ``Storage`` class, so
a schema change on one side fails these tests rather than producing a report
that quietly counts nothing.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import types
from pathlib import Path

import pytest

from findpic.bot.storage import Person, Storage

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bot-stats.py"


def load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("bot_stats", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stats = load()


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def database(tmp_path: Path) -> Path:
    """A small history: two people, one of whom sends photos from an iPhone."""
    path = tmp_path / "findpic-bot.sqlite3"
    storage = Storage(path)
    await storage.connect()

    olena = Person(1001, "olena", "Олена", None, "uk", is_premium=True)
    stranger = Person(1002, None, "Stranger", None, "en")

    start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    for day in range(10):
        for hour in (9, 13, 18):
            event = await storage.record_event(
                olena, kind="file", action=".jpg", chat_type="private"
            )
            await storage.db.execute(
                "UPDATE events SET at = ? WHERE id = ?",
                ((start + dt.timedelta(days=day, hours=hour)).isoformat(), event),
            )
            await storage.note_analysis(
                event,
                make="Apple",
                model="iPhone 13 Pro",
                os_version="17.4.1",
                sent_as="file",
                file_type="JPEG",
                stripped=False,
            )

    refused = await storage.record_event(stranger, kind="photo", chat_type="private")
    await storage.note_outcome(refused, "quota")
    served = await storage.record_event(stranger, kind="photo", chat_type="private")
    await storage.note_analysis(
        served,
        make=None,
        model=None,
        os_version=None,
        sent_as="photo",
        file_type="JPEG",
        stripped=True,
    )
    await storage.record_event(stranger, kind="command", action="help")
    await storage.db.commit()
    await storage.close()
    return path


# ---------------------------------------------------------------- the guesses


def test_the_timezone_guess_recovers_a_planted_offset() -> None:
    """Somebody awake 08:00–22:00 their time, seen from UTC."""
    for offset in (-8, -3, 0, 2, 5, 9):
        hours: collections.Counter[int] = collections.Counter()
        for local in range(8, 23):
            hours[(local - offset) % 24] += 4
        assert stats.guess_offset(hours) == pytest.approx(offset, abs=1)


def test_no_guess_from_activity_spread_round_the_clock() -> None:
    """A bot, a script, or somebody who never sleeps: say nothing, not UTC+0."""
    hours = collections.Counter(dict.fromkeys(range(24), 10))
    assert stats.guess_offset(hours) is None


def test_no_guess_from_a_handful_of_messages() -> None:
    assert stats.guess_offset(collections.Counter({9: 1, 10: 2})) is None


# -------------------------------------------------------------- the terminal


def test_a_hostile_display_name_cannot_drive_the_terminal() -> None:
    """Telegram names are chosen by strangers and printed to a terminal.

    An escape sequence in one would otherwise move the cursor, repaint the
    screen, or hide the rest of the report.
    """
    cleaned = stats.safe("\x1b[2J\x1b[31mwiped\x07\nsecond line")
    assert "\x1b" not in cleaned
    assert "\n" not in cleaned
    assert "\x07" not in cleaned
    assert "wiped" in cleaned


def test_a_long_name_is_cut_to_the_column() -> None:
    assert len(stats.safe("Ω" * 80, 12)) == 12


def test_the_hour_ruler_lines_up_with_the_histogram() -> None:
    ruler = stats.hour_axis()
    assert ruler.index("0") == 0
    assert ruler.index("6") == 6
    assert ruler.index("12") == 12
    assert ruler.index("18") == 18
    assert len(stats.sparkline([1] * 24)) == 24


# ------------------------------------------------------------- the gathering


async def test_the_report_counts_people_and_their_work(database: Path) -> None:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    gathered = stats.collect(conn, None, dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc))
    conn.close()

    assert gathered["people"]["known"] == 2
    assert gathered["people"]["active"] == 2
    assert gathered["use"]["interactions"] == 33
    assert gathered["use"]["analysed"] == 31
    assert gathered["use"]["stripped_on_arrival"] == 1

    olena = next(row for row in gathered["roster"] if row["user_id"] == 1001)
    assert olena["username"] == "olena"
    assert olena["is_premium"] is True
    assert olena["photos"] == 30
    assert olena["active_days"] == 10
    assert olena["devices"][0] == {"make": "Apple", "model": "iPhone 13 Pro", "photos": 30}

    stranger = next(row for row in gathered["roster"] if row["user_id"] == 1002)
    assert stranger["username"] is None
    assert stranger["refused"] == 1

    assert gathered["devices"][0]["os"] == {"17.4.1": 30}
    assert gathered["outcomes"] == {"quota": 1}
    assert gathered["sent_as"] == {"file": 30, "photo": 1}
    assert gathered["client_languages"] == {"uk": 1, "en": 1}


async def test_the_window_excludes_older_activity(database: Path) -> None:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    since = dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc).isoformat()
    gathered = stats.collect(conn, since, dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc))
    conn.close()
    # Two of Olena's ten days survive the cut; the stranger's rows are stamped
    # now rather than backdated, so they stay.
    assert gathered["use"]["interactions"] == 9
    assert gathered["people"]["known"] == 2, "everyone the bot ever met is still counted"


def test_a_database_from_an_older_build_says_so(tmp_path: Path) -> None:
    """Running this before redeploying should explain itself, not traceback."""
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, language TEXT)")
    conn.commit()
    with pytest.raises(SystemExit, match="no usage tables"):
        stats.collect(conn, None, dt.datetime.now(dt.timezone.utc))
    conn.close()


# ------------------------------------------------------------------- the CLI


async def test_the_report_names_who_used_it(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(database), "--all", "--no-color"]) == 0
    printed = capsys.readouterr().out
    assert "@olena" in printed
    assert "1002" in printed
    assert "iPhone 13 Pro" in printed
    assert "17.4.1" in printed
    assert "quota" in printed


async def test_the_json_matches_the_report(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(database), "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["people"]["known"] == 2
    assert {row["user_id"] for row in payload["roster"]} == {1001, 1002}


async def test_one_account_can_be_singled_out(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(database), "--all", "--user", "1001", "--no-color"]) == 0
    printed = capsys.readouterr().out
    assert "@olena" in printed
    assert "iPhone 13 Pro" in printed


async def test_an_unknown_account_is_reported_rather_than_invented(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(database), "--all", "--user", "999"]) == 1
    assert "no activity" in capsys.readouterr().err


async def test_the_roster_exports_as_csv(
    database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "people.csv"
    assert stats.main(["--db", str(database), "--all", "--json", "--csv", str(target)]) == 0
    capsys.readouterr()
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3, "a header and one row per account"
    assert "olena" in lines[1] or "olena" in lines[2]


async def test_the_live_database_is_never_touched(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bot is still writing to this file. Reading it must be a copy.

    Opening a SQLite database with a hot write-ahead log is enough to make
    SQLite rewrite it, which is why the script snapshots first — and why this
    checks the bytes rather than trusting the intent.
    """
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    stats.main(["--db", str(database), "--all", "--json"])
    capsys.readouterr()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


async def test_a_bot_nobody_has_used_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh deployment should read as quiet, not as broken."""
    path = tmp_path / "findpic-bot.sqlite3"
    storage = Storage(path)
    await storage.connect()
    await storage.close()

    assert stats.main(["--db", str(path), "--all", "--no-color"]) == 0
    assert "Nothing recorded" in capsys.readouterr().out
