"""Tests for scripts/bot-stats.py, the usage report.

The script is deliberately standalone — standard library only, so it runs on a
server with nothing installed — which is why it is loaded by path here rather
than imported. Its database is built through the bot's own ``Storage`` class, so
a schema change on one side fails these tests rather than producing a report
that quietly counts nothing.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from findpic.bot.storage import Person, PhotoRecord, Storage

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bot-stats.py"


def load() -> types.ModuleType:
    """The script, imported by path because it is not on any import path.

    Registered in ``sys.modules`` before it is executed, which is the contract
    ``exec_module`` documents and which this helper used to skip. Anything in
    the script that looks itself up by ``__module__`` — a dataclass resolving
    its own annotations, for one — finds nothing otherwise and raises during
    import, which reads as a broken script rather than a broken loader.
    """
    spec = importlib.util.spec_from_file_location("bot_stats", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
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


# ---------------------------------------------------------------- the ledger


@pytest.fixture
async def ledger(tmp_path: Path) -> Path:
    """A history written the way the bot writes it now: one row per picture.

    Deliberately varied, because every section of the report is a projection of
    these rows: three accounts, two cameras and one whose model name is drawn
    two columns wide per character, GPS on some, refusals, a failure, and an
    archive that stored, deduplicated and refused a file each.
    """
    path = tmp_path / "findpic-bot.sqlite3"
    storage = Storage(path)
    await storage.connect()

    olena = Person(2001, "olena", "Олена", "K", "uk", is_premium=True)
    stranger = Person(2002, None, "Stranger", None, "en")
    kenji = Person(2003, "kenji_t", "健太", "田中", "ja")
    start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)

    async def send(
        person: Person,
        when: dt.datetime,
        *,
        kind: str = "file",
        outcome: str | None = None,
        **columns: object,
    ) -> int:
        event = await storage.record_event(person, kind=kind, action=".jpg", chat_type="private")
        await storage.db.execute(
            "UPDATE events SET at = ? WHERE id = ?",
            (when.isoformat(timespec="seconds"), event),
        )
        if outcome:
            await storage.note_outcome(event, outcome)
            return 0
        return await storage.record_photo(
            PhotoRecord(
                user_id=person.user_id,
                at=when.isoformat(timespec="seconds"),
                event_id=event,
                chat_type="private",
                **columns,  # type: ignore[arg-type]
            )
        )

    # Olena: twelve pictures across six days, awake 07:00–19:00 UTC.
    for day in range(6):
        for index, hour in enumerate((7, 12, 19)[: 2 + (day % 2)]):
            has_gps = index == 0
            photo = await send(
                olena,
                start + dt.timedelta(days=day, hours=hour),
                sent_as="file",
                file_type="JPEG",
                mime_type="image/jpeg",
                claimed_name=f"IMG_{100 + day}{index}.jpg",
                size_bytes=2_000_000 + day,
                make="samsung",
                model="SM-S918B",
                os="One UI 6.1",
                lens="Galaxy S23 Ultra main",
                editor="Adobe Lightroom 7.1" if index == 1 else None,
                originality="poor" if index == 1 else "good",
                privacy="bad" if has_gps else "good",
                structure="good",
                tag_count=90,
                finding_ids=json.dumps(["privacy.gps_location"] if has_gps else []),
                had_gps=int(has_gps),
                has_serial=int(index == 0),
                clean_offered=int(has_gps),
                duration_ms=300 + day * 10,
                taken_date="2026-07-15",
                taken_offset="+03:00",
                age_days=17,
                place="Lviv, Lvivska oblast" if has_gps else None,
                country="UA" if has_gps else None,
                lat=49.84 if has_gps else None,
                lon=24.03 if has_gps else None,
            )
            if has_gps:
                await storage.note_photo_action(photo, "clean")
            if day < 3 and index == 0:
                # Two distinct blobs and one repeat, so deduplication has
                # something to report and the totals cannot be a plain SUM.
                digest = f"{'ab' * 16}{day % 2:02d}"
                await storage.note_archived(
                    photo,
                    sha256=digest,
                    bytes_kept=2_000_000,
                    state="stored" if day < 2 else "duplicate",
                    rel_path=f"2026/08/kept-{day}.jpg",
                )
            elif day == 4 and index == 0:
                await storage.note_archived(
                    photo, sha256=None, bytes_kept=0, state="skipped_big", rel_path=None
                )

    # The stranger sends compressed photos, hits the quota, and breaks one.
    for day in (2, 3, 4):
        await send(
            stranger,
            start + dt.timedelta(days=day, hours=22),
            kind="photo",
            sent_as="photo",
            file_type="PNG",
            make="Apple",
            model="iPhone X",
            os="iOS 14.4",
            originality="fair",
            privacy="good",
            structure="fair",
            tag_count=12,
            duration_ms=1500,
            errors=1 if day == 4 else 0,
        )
    await send(stranger, start + dt.timedelta(days=5, hours=22), kind="photo", outcome="quota")
    await send(stranger, start + dt.timedelta(days=5, hours=23), kind="file", outcome="unreadable")

    # A model name whose every character is drawn two columns wide.
    for day in (1, 2):
        await send(
            kenji,
            start + dt.timedelta(days=day, hours=3),
            sent_as="file",
            file_type="HEIC",
            make="スマートフォン",
            model="デジタルカメラ一二三四五六七八",
            os="Android 15",
            originality="good",
            privacy="good",
            structure="good",
            tag_count=40,
            duration_ms=200,
        )

    await storage.db.commit()
    await storage.close()
    return path


def widest(printed: str) -> int:
    """The widest rendered line, in terminal columns rather than characters."""
    return max((stats.columns_of(line) for line in printed.splitlines()), default=0)


async def test_the_ledger_puts_one_picture_on_one_line(ledger: Path) -> None:
    conn = sqlite3.connect(ledger)
    conn.row_factory = sqlite3.Row
    gathered = stats.collect(conn, None, dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc))
    conn.close()

    log = gathered["photos_log"]
    assert len(log) == 22, "20 analysed pictures and the 2 that were turned away"
    assert [line["at"] for line in log] == sorted(line["at"] for line in log)

    first = log[0]
    assert first["who"] == "Олена K"
    assert (first["make"], first["model"], first["os"]) == ("samsung", "SM-S918B", "One UI 6.1")
    assert first["had_gps"] is True
    assert first["findings"] == ["privacy.gps_location"]

    refusals = [line for line in log if line["outcome"]]
    assert {line["outcome"] for line in refusals} == {"quota", "unreadable"}
    assert all(line["source"] == "photos" for line in log if not line["outcome"])


async def test_the_ledger_renders_before_anything_else(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(ledger), "--all", "--no-color", "--limit", "0"]) == 0
    printed = capsys.readouterr().out
    assert printed.index("WHAT WAS SENT") < printed.index("WHO")
    assert "SM-S918B" in printed
    assert "One UI 6.1" in printed, "the OS column is wide enough for a Samsung"
    assert "· quota — hit the daily limit" in printed, "refusals sit inline, in order"
    assert "GPS serial" in printed


async def test_the_ledger_falls_back_to_the_older_schema(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bot deployed before the photos table still gets a ledger.

    The `database` fixture writes through note_analysis, which welds the camera
    onto the interaction row and never touches `photos`. The report has to read
    those rows and say where they came from — an empty findings column there
    means "that build never recorded it", not "the photograph had none".
    """
    assert stats.main(["--db", str(database), "--all", "--no-color"]) == 0
    printed = capsys.readouterr().out
    assert "iPhone 13 Pro" in printed
    assert "read from the older" in printed
    assert "17.4.1" in printed


async def test_a_double_width_model_name_cannot_shift_the_columns(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CJK model name is two terminal columns per character, not one.

    Counting Python characters is what let one photograph's metadata push every
    column to its right off the edge of the screen.
    """
    assert stats.main(["--db", str(ledger), "--all", "--no-color", "--limit", "0"]) == 0
    printed = capsys.readouterr().out
    assert "スマートフォン" in printed
    assert widest(printed) <= 80

    assert stats.columns_of("デジタルカメラ") == 14
    assert stats.columns_of(stats.cell("デジタルカメラ一二三四五六七八", 12)) == 12
    assert stats.columns_of(stats.cell("SM-S918B", 12)) == 12


async def test_the_report_fits_an_eighty_column_terminal(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Piped output takes the narrow layout, because nobody knows where it lands."""
    archive = ledger.parent / "archive"
    (archive / "2026" / "08").mkdir(parents=True)
    (archive / "2026" / "08" / "kept-0.jpg").write_bytes(b"\xff\xd8\xff")
    assert (
        stats.main(
            ["--db", str(ledger), "--all", "--no-color", "--limit", "0", "--archive", str(archive)]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert widest(printed) <= 80
    assert "(gone)" in printed, "kept-1.jpg was never written, and is not pretended into existence"
    assert "kept · " in printed


# ---------------------------------------------------------------- the filters


def gather(path: Path, **narrowing: object) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return stats.collect(
            conn,
            narrowing.pop("since", None),  # type: ignore[arg-type]
            dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
            filters=stats.Filters(**narrowing),  # type: ignore[arg-type]
        )
    finally:
        conn.close()


async def test_a_filter_narrows_every_section_at_once(ledger: Path) -> None:
    """One place, before the counters, or the headline stops matching the tables.

    Filtering per section is how a report ends up saying nineteen interactions
    over a table that lists three: the counters were fed the unfiltered rows.
    """
    narrowed = gather(ledger, device="iphone")
    assert {line["model"] for line in narrowed["photos_log"]} == {"iPhone X"}
    assert narrowed["use"]["interactions"] == len(narrowed["photos_log"])
    assert narrowed["use"]["analysed"] == 3
    assert [row["user_id"] for row in narrowed["roster"]] == [2002]
    assert narrowed["devices"][0]["model"] == "iPhone X"
    assert narrowed["filters"] == ["device ~ iphone"]


async def test_every_filter_selects_what_it_names(ledger: Path) -> None:
    everything = gather(ledger)
    assert len(everything["photos_log"]) == 22

    since = gather(ledger, since="2026-08-05T00:00:00+00:00")
    assert all(line["at"] >= "2026-08-05" for line in since["photos_log"])
    assert len(since["photos_log"]) < 22

    until = gather(ledger, until="2026-08-02")
    assert all(line["at"][:10] <= "2026-08-02" for line in until["photos_log"])

    by_name = gather(ledger, users=("@olena",))
    assert {line["user_id"] for line in by_name["photos_log"]} == {2001}
    assert gather(ledger, users=("2001",))["people"]["known"] == 1

    assert {line["os"] for line in gather(ledger, system="one ui")["photos_log"]} == {"One UI 6.1"}
    assert all(line["had_gps"] for line in gather(ledger, gps=True)["photos_log"])
    assert not any(line["had_gps"] for line in gather(ledger, gps=False)["photos_log"])
    assert {line["sent_as"] for line in gather(ledger, sent_as="photo")["photos_log"]} == {"photo"}

    trouble = gather(ledger, failed=True)["photos_log"]
    assert {line["outcome"] for line in trouble} == {"quota", "unreadable", None}
    assert all(line["outcome"] or line["errors"] for line in trouble)

    quiet = gather(ledger, min_events=6)
    assert [row["user_id"] for row in quiet["roster"]] == [2001], "only Olena sent that many"


async def test_an_unknown_username_is_refused_rather_than_ignored(ledger: Path) -> None:
    with pytest.raises(SystemExit, match="username"):
        gather(ledger, users=("@nobody",))


async def test_a_filtered_report_says_so_where_it_cannot_be_missed(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(ledger), "--all", "--no-color", "--with-gps"]) == 0
    printed = capsys.readouterr().out
    assert "filters   with GPS" in printed


async def test_the_ledger_can_be_printed_on_its_own(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(ledger), "--all", "--no-color", "--photos"]) == 0
    printed = capsys.readouterr().out
    assert "WHO" not in printed
    assert printed.count("SM-S918B") == 15, "--photos runs to full length, ignoring --limit"


async def test_json_can_single_out_one_account(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json used to ignore --user, because it returned before that branch.

    The filters live inside collect() now, so there is only one place where a
    row can be dropped and every output path goes through it.
    """
    assert stats.main(["--db", str(ledger), "--all", "--json", "--user", "2002"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["user_id"] for row in payload["roster"]] == [2002]
    assert {line["user_id"] for line in payload["photos_log"]} == {2002}
    assert payload["people"]["known"] == 1


async def test_every_picture_exports_as_its_own_csv_row(
    ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "photos.csv"
    assert stats.main(["--db", str(ledger), "--all", "--json", "--csv-photos", str(target)]) == 0
    capsys.readouterr()
    rows = list(csv.DictReader(target.open(encoding="utf-8")))
    assert len(rows) == 22
    assert rows[0]["who"] == "Олена K"
    assert rows[0]["model"] == "SM-S918B"
    assert rows[0]["findings"] == "privacy.gps_location"
    assert any(row["outcome"] == "quota" for row in rows)
    assert any(row["state"] == "stored" for row in rows)


def test_a_formula_in_a_camera_name_cannot_run_in_a_spreadsheet() -> None:
    """Every column in the picture export comes out of somebody's photograph."""
    assert stats._cell("=cmd|'/c calc'!A1").startswith("'=")
    assert stats._cell("@SUM(A1)").startswith("'@")
    assert "\x1b" not in stats._cell("\x1b[2Jmodel")


# ------------------------------------------------------------ the zone guess


def test_the_timezone_guess_states_how_wide_it_is() -> None:
    """A guess with no width is a claim. The width is the whole answer.

    ``waking hours unknown`` was what the owner saw, because the old gate wanted
    fifteen messages and threw the concentration away instead of reporting it.
    """
    awake = collections.Counter({(local - 3) % 24: 4 for local in range(8, 23)})
    spread = stats.guess_zone(awake)
    assert spread is not None
    assert spread["offset"] == 3
    assert spread["width"] == 3
    assert spread["messages"] == 60

    # Five messages over one lunch break point somewhere very precisely and are
    # still five messages: the width has to come back out.
    thin = stats.guess_zone(collections.Counter({13: 2, 14: 2, 15: 1}))
    assert thin is not None
    assert thin["width"] >= 4
    assert thin["messages"] == 5

    office = stats.guess_zone(collections.Counter(dict.fromkeys(range(9, 19), 5)))
    assert office is not None
    assert office["width"] == 1, "ten hours of office traffic is a real answer"

    assert stats.guess_zone(collections.Counter({9: 1, 10: 2})) is None
    assert stats.guess_zone(collections.Counter(dict.fromkeys(range(24), 10))) is None


async def test_the_report_gives_every_busy_account_a_zone(
    ledger: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stats.main(["--db", str(ledger), "--all", "--no-color", "--limit", "0"]) == 0
    printed = capsys.readouterr().out
    assert "waking hours" in printed
    assert "awake ≈ UTC" in printed
    assert "±" in printed
    assert "msgs, " in printed, "the evidence sits next to the guess"
    assert "UTC+2 2" not in printed, "the aggregate histogram named nobody and is gone"


# ------------------------------------------------------------- exporting files


def a_row(**over) -> dict:
    row = {
        "rel_path": "by-date/2026-08-29/20260829T153937Z-u7-01dde9b2.jpg",
        "at": "2026-08-29T15:39:37+00:00",
        "user_id": 7,
        "username": "someone",
        "make": "Apple",
        "model": "iPhone X",
        "stripped": False,
    }
    row.update(over)
    return row


def test_the_export_names_a_folder_after_the_sender() -> None:
    assert stats.who_directory(a_row()) == "someone-7"


def test_the_numeric_id_stays_in_the_folder_name() -> None:
    """A username can be given up and taken over by somebody else.

    Without the id, one person renaming themselves would look like two people,
    and their older pictures would sit in a folder belonging to whoever has the
    name now.
    """
    assert "7" in stats.who_directory(a_row())


def test_a_hostile_username_never_becomes_a_directory() -> None:
    """This is a real path on the operator's own machine."""
    for handle in ("../../etc", "a/b", "..", "", None, "x" * 200, "ім'я", "a\x00b"):
        folder = stats.who_directory(a_row(username=handle))
        assert folder == "id7", handle


def test_a_display_name_is_never_used_as_a_folder() -> None:
    """Display names are arbitrary Unicode chosen by a stranger."""
    assert stats.who_directory(a_row(username=None, who="../../root")) == "id7"


def test_the_exported_name_reads_as_a_photograph() -> None:
    name = stats.export_name(a_row())
    assert name.startswith("2026-08-29 15-39")
    assert "Apple iPhone X" in name
    assert name.endswith("01dde9b2.jpg"), "the digest joins it back to the ledger"


def test_a_stripped_photo_says_so_in_its_name() -> None:
    assert "no camera" in stats.export_name(a_row(make=None, model=None, stripped=True))


def test_an_exported_name_can_never_contain_a_separator() -> None:
    name = stats.export_name(a_row(make="Ev/il", model="..\\x"))
    assert "/" not in name and "\\" not in name


def test_the_plan_comes_from_the_manifest_not_the_archive(tmp_path: Path) -> None:
    """A tar from a machine this script does not control must not choose its
    own filenames — so the destination is derived from the database row."""
    payload = {"photos_log": [a_row(), a_row(rel_path=None)]}
    plan = stats.export_plan(payload, tmp_path)
    assert len(plan) == 1, "a row with no file is not fetched"
    rel, destination = plan[0]
    assert rel == a_row()["rel_path"]
    assert destination.is_relative_to(tmp_path)


def test_a_local_export_copies_the_files(tmp_path: Path) -> None:
    root = tmp_path / "archive" / "by-date" / "2026-08-29"
    root.mkdir(parents=True)
    (root / "20260829T153937Z-u7-01dde9b2.jpg").write_bytes(b"\xff\xd8\xff picture")

    into = tmp_path / "out"
    written, missing = stats.export_photos(
        {"photos_log": [a_row()]}, into, root=str(tmp_path / "archive"), remote=None
    )
    assert (written, missing) == (1, 0)
    landed = list(into.rglob("*.jpg"))
    assert len(landed) == 1
    assert landed[0].read_bytes() == b"\xff\xd8\xff picture"
    assert landed[0].parent.parent.name == "someone-7"


def test_a_missing_file_is_counted_rather_than_crashing(tmp_path: Path) -> None:
    written, missing = stats.export_photos(
        {"photos_log": [a_row()]}, tmp_path / "out", root=str(tmp_path / "gone"), remote=None
    )
    assert (written, missing) == (0, 1)
