#!/usr/bin/env python3
"""Who used the findpic bot: every picture, who sent it, and what it carried.

Point it at the bot's SQLite database — a local copy, or pulled straight out of
the server's Docker volume over ssh:

    scripts/bot-stats.py --db /data/findpic-bot.sqlite3
    scripts/bot-stats.py --ssh you@your.server
    scripts/bot-stats.py --ssh you@your.server --all --json > stats.json
    scripts/bot-stats.py --ssh you@your.server --user 5829771410
    scripts/bot-stats.py --ssh you@your.server --photos --limit 0

The report opens with the ledger — one line per photograph, in the order they
arrived, refusals included. Everything after it is a summary of those same
lines. That ordering is the point: this script used to shred every row into
eight independent counters and print only the totals, so WHO listed people
without cameras, DEVICES listed cameras without people, and no admin could ever
answer "who sent that, from what, and when" without opening SQLite themselves.

**What Telegram does not tell a bot, and no script can recover.** There is no
device, no operating system, no app version, no IP address and no location
anywhere in what the Bot API hands over. A bot sees an *account* — id, name,
username, the language the client asks in, the premium flag — and the moment
each message arrived. That is the entire list. Device and OS are visible only in
Telegram's own "Devices" screen, for your own account, and there is no API for
anybody else's.

So the two columns people ask for first are answered here from elsewhere, and
labelled for what they are:

* **Devices and OS** are read out of the photographs themselves, by findpic. It
  is a claim about the camera that took the picture, which is usually the phone
  in the sender's hand but is not the same statement — and it is blank for
  everyone whose photos arrived already stripped.
* **Where** has three proxies of very different quality, and they are printed in
  three different places on purpose. The language the Telegram client asks in;
  the hours of the day somebody is active, which puts their waking day somewhere
  on the clock and is a guess with a stated width; and the country recorded in a
  photograph's own GPS tags, which is a fact about the picture and says nothing
  about where the sender was sitting when they sent it.

Standard library only, so it also runs on the server with nothing installed.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import io
import json
import math
import os
import shlex
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DB_NAME = "findpic-bot.sqlite3"

#: Defaults matching deploy/docker-compose.yml — project "findpic", volume
#: "findpic-data". Docker prefixes the project name onto the volume.
DEFAULT_VOLUME = "findpic_findpic-data"
DEFAULT_IMAGE = "alpine:3.20"

#: Where a local run looks, in order, when --db is not given.
DB_CANDIDATES = (
    Path("/data") / DB_NAME,
    Path(DB_NAME),
    Path("deploy") / DB_NAME,
    Path.home() / ".local" / "share" / "findpic" / DB_NAME,
)

#: Outcomes the bot writes when it declines to do the work, against what they
#: mean in a sentence. Anything not listed here was served.
REFUSALS = {
    "blocked": "not on the allowlist",
    "throttled": "sent them too fast",
    "quota": "hit the daily limit",
}
FAILURES = {
    "too_big": "file over the size limit",
    "not_an_image": "not an image",
    "unreadable": "exiftool could not read it",
    "failed": "the bot crashed on it",
}

#: What became of the copy the archive tried to keep, in a sentence.
ARCHIVE_STATES = {
    "stored": "kept",
    "duplicate": "already had that file",
    "skipped_big": "over the size limit",
    "skipped_space": "no room left",
    "error": "the copy failed",
    "evicted": "deleted to make room",
}

#: The verdict levels findpic writes, worst last. `unknown` is deliberately
#: outside the ranking: it means nobody looked, not that the answer was bad.
LEVEL_RANK = {"good": 0, "fair": 1, "poor": 2, "bad": 3}

#: An interaction that was an attempt to send a picture. Used for one thing
#: only: deciding whether a *refused* event belongs in the picture ledger. A
#: refusal never reaches an analysis, so it has no `sent_as` to filter on and
#: `kind` is the only signal left. Nothing else in this script filters on kind —
#: classify() calls every document a "file", so a PDF is already in that count
#: and a ledger built on it would list things that are not pictures.
UPLOAD_KINDS = {"photo", "file", "media"}

BLOCKS = "▁▂▃▄▅▆▇█"

#: The local hour a person's messaging day balances around, used to turn an
#: average activity hour into a timezone. Mid-afternoon: late enough that the
#: morning and the evening both pull on it, early enough not to sit in the tail.
ACTIVITY_CENTRE = 15.0

#: How concentrated somebody's activity has to be for the offset guess to be
#: worth what width. Read as "at least this concentrated, so ± this many hours".
#:
#: These numbers are a judgement call, not a derivation. They are anchored on
#: two measured shapes: somebody awake 08:00–22:00 and writing evenly across it
#: scores about 0.47, which is the commonest real pattern and deserves the ±3h
#: in the middle of the table; somebody who only writes during office hours
#: scores about 0.74 and has genuinely told you their timezone to within an
#: hour. The floor band, ±6h, covers half the world and is the honest reading of
#: activity with no shape to it at all.
OFFSET_BANDS = ((0.70, 1), (0.55, 2), (0.40, 3), (0.28, 4), (0.15, 5), (0.0, 6))

#: A tight-looking cluster of five messages is not five messages' worth of
#: evidence — it is one lunch break. Below each count the band above cannot
#: narrow past the matching width, whatever the concentration says. Also a
#: judgement call.
THIN_EVIDENCE = ((6, 4), (10, 3), (20, 2))

#: Fewer messages than this and there is nothing to average. The old code
#: demanded 15, which is why the owner's busiest account — seven messages —
#: reported "waking hours unknown" and the whole section said nothing.
MIN_MESSAGES_FOR_GUESS = 4

#: Below this the activity vector has a length but no direction: somebody who
#: writes at every hour of the clock. Say nothing rather than point at noon.
NO_DIRECTION = 0.05

#: The report is laid out for a terminal this wide and grows into a wider one.
#: Piped output gets the narrow one — a redirected report is read later, in
#: something whose width nobody knows.
NARROW = 80
WIDEST = 140

#: How much of an attacker-controlled string reaches a file. The terminal clips
#: harder, to its column; this is the cap for --json and --csv, which have no
#: columns to protect but should not carry a megabyte of somebody's filename.
EXPORT_LIMIT = 120


def hour_axis() -> str:
    """A 24-column ruler under an hour histogram, one character per hour."""
    row = [" "] * 26
    for hour in (0, 6, 12, 18):
        for step, digit in enumerate(str(hour)):
            row[hour + step] = digit
    return "".join(row).rstrip()


# --------------------------------------------------------------- the terminal


class Ink:
    """Just enough ANSI to make the report scannable, and none when piped."""

    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)


def safe(text: str | None, limit: int = 0) -> str:
    """A display name, made safe to print.

    Telegram names are chosen by the person on the other end and can hold
    newlines, control characters and terminal escape sequences. Printing one
    straight into a terminal hands a stranger control of the cursor, so every
    unprintable character is replaced before it gets there.

    Every camera name, lens, editor, filename and place in this report comes out
    of a stranger's photograph and is no more trustworthy than a display name,
    so all of them go through here on the way out of the database — once, in
    collect(), rather than once per section.
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in (text or "")).strip()
    if limit and len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def columns_of(text: str) -> int:
    """How many terminal columns a string occupies.

    Counting characters is what broke the tables: one CJK or emoji model name
    is drawn two columns wide per character and shunted every column to its
    right. East-asian Wide and Fullwidth characters are counted as two here,
    which is what every terminal does. It is still an approximation — combining
    marks and ZWJ emoji sequences are drawn differently by every terminal
    emulator there is, and no library can be right about all of them — so the
    table cells also clip, to bound how far a hostile name can push.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def clip(text: str, width: int) -> str:
    """Cut a string to a display width, marking that something was cut."""
    if columns_of(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for ch in text:
        step = columns_of(ch)
        if used + step > width - 1:
            break
        kept.append(ch)
        used += step
    return "".join(kept) + "…"


def pad(text: str, width: int) -> str:
    """Left-align in a fixed column, counting display columns."""
    return text + " " * max(0, width - columns_of(text))


def cell(text: str | None, width: int) -> str:
    """One table cell: sanitised, clipped to its column, padded out to it."""
    return pad(clip(safe(text), width), width)


def bar(value: int, peak: int, width: int = 26) -> str:
    if peak <= 0:
        return ""
    filled = round(width * value / peak)
    return "█" * filled if filled else ("▏" if value else "")


def sparkline(values: list[int]) -> str:
    peak = max(values) if values else 0
    if peak <= 0:
        return " " * len(values)
    return "".join(BLOCKS[min(7, round(7 * v / peak))] if v else " " for v in values)


def terminal_width(stream: Any = None) -> int:
    """The width to lay the tables out for, clamped to something readable.

    A pipe has no width, and `shutil` would happily answer with whatever
    COLUMNS says the *calling* terminal is — which is not where a redirected
    report will be read. So anything that is not a terminal gets the narrow
    layout, and the tables are designed to fit it.
    """
    stream = sys.stdout if stream is None else stream
    try:
        interactive = stream.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        return NARROW
    return max(NARROW, min(WIDEST, shutil.get_terminal_size((NARROW, 24)).columns))


def sized(count: int | float | None) -> str:
    """Bytes, in the largest unit that leaves a number worth reading."""
    if not count:
        return "0 B"
    step = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GB"


def took(milliseconds: float | None) -> str:
    if not milliseconds:
        return "—"
    return f"{milliseconds / 1000:.1f} s" if milliseconds >= 1000 else f"{milliseconds:.0f} ms"


def tally(counts: dict[str, int], joiner: str = " · ") -> str:
    return joiner.join(f"{name} {count}" for name, count in counts.items())


# ------------------------------------------------------------ getting the file


def volume_command(volume: str, image: str) -> list[str]:
    """Stream the database out of a Docker volume as a tar on stdout.

    A throwaway container rather than a path, because the volume's directory
    under /var/lib/docker belongs to root: anybody in the docker group can read
    it this way without sudo, and it is the same command whether it runs here or
    at the far end of an ssh connection. Mounted read-only, so it cannot disturb
    the running bot — the worst it can do is catch the database mid-write, which
    SQLite's write-ahead log is designed to survive.

    ``cd`` rather than ``tar -C``: the glob is expanded by the container's shell
    before tar ever sees it, and would otherwise look in the container's root.
    """
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume}:/data:ro",
        image,
        "sh",
        "-c",
        f"cd /data && tar -cf - {DB_NAME}*",
    ]


def pull(argv: list[str], into: Path, source: str, volume: str) -> Path:
    """Run a tar-producing command and unpack the database out of its output."""
    print(f"pulling {DB_NAME} from {source} …", file=sys.stderr)
    result = subprocess.run(argv, capture_output=True, check=False)
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SystemExit(
            f"could not read {source}: "
            + (detail[-1] if detail else "no output")
            + f"\n  check the volume name (docker volume ls) — assumed {volume!r}"
        )

    # The tar may come from a machine this script does not control, so members
    # are unpacked by hand: regular files only, basenames only, nothing else.
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive:
            name = Path(member.name).name
            if not member.isfile() or not name.startswith(DB_NAME):
                continue
            handle = archive.extractfile(member)
            if handle is not None:
                (into / name).write_bytes(handle.read())

    database = into / DB_NAME
    if not database.exists():
        raise SystemExit(f"{source} holds no {DB_NAME}")
    return database


def snapshot(database: Path, into: Path) -> Path:
    """Work on a copy, always.

    The live file belongs to a running bot. Opening it read-only would still be
    enough to recover a hot write-ahead log, which is a write; copying it first
    means this script cannot touch the bot's data even by accident.
    """
    if not database.exists():
        raise SystemExit(f"no database at {database}")
    copy = into / DB_NAME
    shutil.copyfile(database, copy)
    wal = database.with_name(database.name + "-wal")
    if wal.exists():
        # Most of the recent activity lives here until SQLite checkpoints it.
        shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
    return copy


def find_database() -> Path:
    for candidate in DB_CANDIDATES:
        if candidate.exists():
            return candidate
    raise SystemExit(
        "no database found. Pass --db PATH, or --ssh HOST to pull it off the server.\n"
        "  looked in: " + ", ".join(str(c) for c in DB_CANDIDATES)
    )


# ------------------------------------------------------------------- the guess


def guess_zone(
    hours: collections.Counter[int], minimum: int = MIN_MESSAGES_FOR_GUESS
) -> dict[str, Any] | None:
    """Somebody's UTC offset and how wide the guess is, from when they write.

    Hours are a circle, so the average of one is a vector sum rather than an
    arithmetic mean — otherwise 23:00 and 01:00 average to noon. Taking that
    mean and calling it mid-afternoon local time gives an offset.

    The length of that vector is how concentrated the activity is. It used to be
    thrown away behind a hard ``< 0.15`` gate, which is why an account with
    seven messages got "unknown" and the operator got nothing at all: the
    concentration is not a pass mark, it is the *width* of the answer. It comes
    back here as ``width`` in hours, along with the evidence it was computed
    from, so that a reader can disbelieve a particular row rather than having to
    distrust the whole column.

    Not a location. Shift work, insomnia, travel and a single 06:xx message all
    move it, which is exactly why the width is printed next to it.
    """
    total = sum(hours.values())
    if total < minimum:
        return None
    step = 2 * math.pi / 24
    x = sum(count * math.cos(hour * step) for hour, count in hours.items()) / total
    y = sum(count * math.sin(hour * step) for hour, count in hours.items()) / total
    strength = math.hypot(x, y)
    if strength < NO_DIRECTION:
        return None

    width = next(hours_wide for edge, hours_wide in OFFSET_BANDS if strength >= edge)
    for count, floor in THIN_EVIDENCE:
        if total < count:
            width = max(width, floor)
            break

    mean = (math.atan2(y, x) / step) % 24
    seen = sorted(hours)
    return {
        # Fold into the range the world actually uses: UTC-11 to UTC+12.
        "offset": (round(ACTIVITY_CENTRE - mean) + 11) % 24 - 11,
        "width": width,
        "messages": total,
        "strength": round(strength, 3),
        "first_hour": seen[0],
        "last_hour": seen[-1],
    }


def guess_offset(
    hours: collections.Counter[int], minimum: int = MIN_MESSAGES_FOR_GUESS
) -> int | None:
    """Just the offset, for the CSV column and for anyone who wants one number."""
    zone = guess_zone(hours, minimum)
    return zone["offset"] if zone else None


# ------------------------------------------------------------------- gathering


@dataclass(frozen=True)
class Filters:
    """What the operator asked to be left out, in one object.

    Applied once, inside collect(), before any counter sees a row. Filtering per
    section is how a report ends up with a headline that disagrees with its own
    tables — and it is why ``--json --user`` used to ignore the account
    entirely, because the JSON branch returned before the drill-down ran.
    """

    until: str | None = None
    users: tuple[str, ...] = ()
    device: str | None = None
    system: str | None = None
    gps: bool | None = None
    failed: bool = False
    sent_as: str | None = None
    min_events: int = 0

    def picky_about_pictures(self) -> bool:
        """Whether anything here narrows the ledger rather than the accounts.

        When it does, the interaction log is narrowed to the events those
        pictures came from, so the headline counts stay true to the tables.
        """
        return bool(
            self.device or self.system or self.sent_as or self.failed or self.gps is not None
        )

    def spoken(self) -> list[str]:
        """The active filters, for the line under the window."""
        said = []
        if self.users:
            said.append("user " + ", ".join(self.users))
        if self.until:
            said.append(f"until {self.until}")
        if self.device:
            said.append(f"device ~ {safe(self.device, 24)}")
        if self.system:
            said.append(f"os ~ {safe(self.system, 24)}")
        if self.gps is not None:
            said.append("with GPS" if self.gps else "without GPS")
        if self.sent_as:
            said.append(f"sent as {safe(self.sent_as, 8)}")
        if self.failed:
            said.append("refused or failed only")
        if self.min_events:
            said.append(f"at least {self.min_events} interactions")
        return said


def _text(value: Any) -> str | None:
    """One attacker-controlled column, cleaned on the way out of the database."""
    if value is None:
        return None
    return safe(str(value), EXPORT_LIMIT) or None


def _findings(raw: Any) -> list[str]:
    """The stable finding ids, out of the JSON array the bot stored.

    Garbage in this column is a bug in the bot, not a reason for the operator's
    report to traceback in the middle of a table.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [safe(str(item), 48) for item in parsed if item] if isinstance(parsed, list) else []


def _from_photo(row: dict[str, Any]) -> dict[str, Any]:
    """One ledger line out of a `photos` row."""
    return {
        "source": "photos",
        "photo_id": row["id"],
        "event_id": row["event_id"],
        "user_id": int(row["user_id"]),
        "at": row["at"],
        "outcome": None,
        "sent_as": _text(row["sent_as"]),
        "chat_type": _text(row["chat_type"]),
        "file_type": _text(row["file_type"]),
        "mime_type": _text(row["mime_type"]),
        "claimed_name": _text(row["claimed_name"]),
        "size_bytes": row["size_bytes"],
        "width": row["width"],
        "height": row["height"],
        "make": _text(row["make"]),
        "model": _text(row["model"]),
        "os": _text(row["os"]),
        "lens": _text(row["lens"]),
        "editor": _text(row["editor"]),
        "originality": _text(row["originality"]),
        "privacy": _text(row["privacy"]),
        "structure": _text(row["structure"]),
        "tag_count": row["tag_count"],
        "findings": _findings(row["finding_ids"]),
        "stripped": bool(row["stripped"]),
        "had_gps": bool(row["had_gps"]),
        "has_serial": bool(row["has_serial"]),
        "clean_offered": bool(row["clean_offered"]),
        "tags_taken": int(row["tags_taken"] or 0),
        "clean_taken": int(row["clean_taken"] or 0),
        "backup_taken": int(row["backup_taken"] or 0),
        "duration_ms": row["duration_ms"],
        "warnings": int(row["warnings"] or 0),
        "errors": int(row["errors"] or 0),
        "taken_date": _text(row["taken_date"]),
        "taken_offset": _text(row["taken_offset"]),
        "age_days": row["age_days"],
        "place": _text(row["place"]),
        "country": _text(row["country"]),
        "lat": row["lat"],
        "lon": row["lon"],
        "sha256": _text(row["sha256"]),
        "bytes_kept": row["bytes_kept"],
        "state": _text(row["state"]),
        "rel_path": _text(row["rel_path"]),
    }


def _from_event(row: dict[str, Any]) -> dict[str, Any]:
    """One ledger line out of the columns welded onto an old `events` row.

    Everything the newer schema records and this one does not is left None
    rather than filled with a zero. A blank column here means "that build never
    wrote it down", and the report says so under the table — a 0 would read as
    "the photograph had none", which is a different and false claim.
    """
    line: dict[str, Any] = {
        "source": "events",
        "photo_id": None,
        "event_id": row["id"],
        "user_id": int(row["user_id"]),
        "at": row["at"],
        "outcome": _text(row["outcome"]),
        "sent_as": _text(row["sent_as"]),
        "chat_type": _text(row["chat_type"]),
        "file_type": _text(row["file_type"]),
        "make": _text(row["make"]),
        "model": _text(row["model"]),
        "os": _text(row["os"]),
        "stripped": bool(row["stripped"]),
        "findings": [],
        "tags_taken": 0,
        "clean_taken": 0,
        "backup_taken": 0,
        "warnings": 0,
        "errors": 0,
    }
    for missing in (
        "mime_type",
        "claimed_name",
        "size_bytes",
        "width",
        "height",
        "lens",
        "editor",
        "originality",
        "privacy",
        "structure",
        "tag_count",
        "had_gps",
        "has_serial",
        "clean_offered",
        "duration_ms",
        "taken_date",
        "taken_offset",
        "age_days",
        "place",
        "country",
        "lat",
        "lon",
        "sha256",
        "bytes_kept",
        "state",
        "rel_path",
    ):
        line[missing] = None
    return line


def _refusal(row: dict[str, Any]) -> dict[str, Any]:
    """A picture that never became one: the bot said no, or the analysis died."""
    line = _from_event(row)
    line["sent_as"] = None
    return line


def _analysed(row: dict[str, Any]) -> dict[str, Any]:
    """An old row that did reach an analysis, whatever else is stamped on it.

    A ledger line is either a picture or a refusal, never both. The handlers
    write one or the other and return, but the report reads databases it did not
    write, and a row carrying a camera *and* an outcome would otherwise be
    counted as turned away while holding the evidence that it was not.
    """
    line = _from_event(row)
    line["outcome"] = None
    return line


def build_ledger(
    events: list[dict[str, Any]], photos: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One row per picture, refusals included, oldest first.

    Three sources, in order of preference. A `photos` row is the real thing. An
    `events` row carrying a `sent_as` is the same picture as recorded by a build
    that predates the photos table, and is used only where no photos row covers
    it — so a database that spans the upgrade reads as one continuous log rather
    than two halves or a pile of duplicates. A refused upload has neither, and
    is included so that the ledger is a chronology and not a success story.
    """
    covered = {row["event_id"] for row in photos if row["event_id"]}
    lines = [_from_photo(row) for row in photos]
    for row in events:
        if row["id"] in covered:
            continue
        if row["sent_as"]:
            lines.append(_analysed(row))
        elif row["outcome"] and (row["kind"] or "") in UPLOAD_KINDS:
            lines.append(_refusal(row))
    lines.sort(key=lambda line: (line["at"], line["user_id"]))
    return lines


def _resolve_users(specs: tuple[str, ...], people: dict[int, dict[str, Any]]) -> set[int] | None:
    """Turn what was typed after --user into account ids.

    A username is accepted because it is what an operator has in front of them
    when somebody complains, and an id is not.
    """
    if not specs:
        return None
    by_name = {
        str(row.get("username") or "").lower(): user_id
        for user_id, row in people.items()
        if row.get("username")
    }
    wanted: set[int] = set()
    for spec in specs:
        text = spec.strip()
        if text.lstrip("-").isdigit():
            wanted.add(int(text))
            continue
        found = by_name.get(text.lstrip("@").lower())
        if found is None:
            raise SystemExit(f"no account here with the username {text!r}")
        wanted.add(found)
    return wanted


def _matches(line: dict[str, Any], picks: Filters) -> bool:
    """Whether one ledger row survives the picture filters."""
    if picks.sent_as and line["sent_as"] != picks.sent_as:
        return False
    if picks.device:
        camera = f"{line['make'] or ''} {line['model'] or ''}".lower()
        if picks.device.lower() not in camera:
            return False
    if picks.system and picks.system.lower() not in str(line["os"] or "").lower():
        return False
    # An older row has no answer to give about GPS. Dropped by both --with-gps
    # and --no-gps rather than counted as a "no", which would invent a fact.
    if picks.gps is not None and (
        line["had_gps"] is None or bool(line["had_gps"]) is not picks.gps
    ):
        return False
    return not (picks.failed and not (line["outcome"] or line["errors"]))


def collect(
    conn: sqlite3.Connection,
    since: str | None,
    now: dt.datetime,
    *,
    filters: Filters | None = None,
) -> dict[str, Any]:
    """Everything the report needs, in one dictionary.

    Both tables are pulled into memory and aggregated in Python. That would be
    wrong for a busy service; for a bot whose entire history is a few thousand
    rows it is far clearer than two dozen GROUP BY queries, and it is what lets
    the filters be applied exactly once — to the rows, before any counter sees
    them — so that a narrowed report's headline agrees with its own tables.

    The per-photo ledger is built first and everything else is a summary of it.
    ``since`` stays a positional argument because it is the window and every
    caller passes one; the rest of the narrowing arrives in ``filters``.
    """
    picks = filters or Filters()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "events" not in tables or "people" not in tables:
        raise SystemExit(
            "this database has no usage tables — it was written by a bot build from\n"
            "before they existed. Redeploy the bot and it will start recording.\n"
            "Until then all that is kept is the language preference in `users`."
        )

    # The bot records its own archive root here, so a report read on a laptop
    # a long way from the disk still prints paths that mean something.
    settings: dict[str, str] = {}
    if "settings" in tables:
        settings = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM settings")}

    people = {int(row["user_id"]): dict(row) for row in conn.execute("SELECT * FROM people")}
    chosen = {
        int(row["user_id"]): row["language"] for row in conn.execute("SELECT * FROM users")
    }
    events = [dict(row) for row in conn.execute("SELECT * FROM events")]
    # Feature-detected rather than assumed: a bot that has not been redeployed
    # has no photos table at all, and one that has just been redeployed has an
    # empty one with a history still living on `events`.
    photographs = (
        [dict(row) for row in conn.execute("SELECT * FROM photos")] if "photos" in tables else []
    )

    wanted = _resolve_users(picks.users, people)
    ledger = build_ledger(events, photographs)

    # ------------------------------------------------ the one place rows drop
    def in_window(stamp: str) -> bool:
        if since and stamp < since:
            return False
        return not (picks.until and stamp[:10] > picks.until)

    def in_scope(row: dict[str, Any]) -> bool:
        return in_window(row["at"]) and (wanted is None or int(row["user_id"]) in wanted)

    events = [row for row in events if in_scope(row)]
    ledger = [line for line in ledger if in_scope(line)]

    if picks.min_events:
        busy = collections.Counter(int(row["user_id"]) for row in events)
        keep = {user_id for user_id, count in busy.items() if count >= picks.min_events}
        events = [row for row in events if int(row["user_id"]) in keep]
        ledger = [line for line in ledger if int(line["user_id"]) in keep]

    if picks.picky_about_pictures():
        ledger = [line for line in ledger if _matches(line, picks)]
        # The interaction log follows the pictures rather than the other way
        # round: --device iphone must not leave a headline counting somebody's
        # /start. Rows the ledger never covered (a menu tap) go with it.
        surviving = {line["event_id"] for line in ledger if line["event_id"]}
        events = [row for row in events if row["id"] in surviving]

    if wanted is not None:
        people = {user_id: row for user_id, row in people.items() if user_id in wanted}

    # ------------------------------------------------------------ the counters
    per_user: dict[int, dict[str, Any]] = {}
    days: collections.Counter[str] = collections.Counter()
    hours: collections.Counter[int] = collections.Counter()
    kinds: collections.Counter[str] = collections.Counter()
    actions: collections.Counter[str] = collections.Counter()
    outcomes: collections.Counter[str] = collections.Counter()
    chats: collections.Counter[str] = collections.Counter()

    def seat_for(user_id: int, stamp: str) -> dict[str, Any]:
        return per_user.setdefault(
            user_id,
            {
                "user_id": user_id,
                "events": 0,
                "photos": 0,
                "analysed": 0,
                "refused": 0,
                "failed": 0,
                "hours": collections.Counter(),
                "days": set(),
                "devices": collections.Counter(),
                "trouble": collections.Counter(),
                "kept": 0,
                "bytes_kept": 0,
                "gps": 0,
                "first": stamp,
                "last": stamp,
            },
        )

    for event in events:
        user_id = int(event["user_id"])
        seat = seat_for(user_id, event["at"])
        seat["events"] += 1
        seat["first"] = min(seat["first"], event["at"])
        seat["last"] = max(seat["last"], event["at"])

        stamp = event["at"]
        day = stamp[:10]
        days[day] += 1
        seat["days"].add(day)
        try:
            hour = dt.datetime.fromisoformat(stamp).astimezone(dt.timezone.utc).hour
        except ValueError:
            hour = int(stamp[11:13]) if len(stamp) > 12 else 0
        hours[hour] += 1
        seat["hours"][hour] += 1

        kind = event["kind"] or "other"
        kinds[kind] += 1
        if event["action"]:
            actions[f"{kind}:{event['action']}"] += 1
        if event["chat_type"]:
            chats[event["chat_type"]] += 1
        if kind in ("photo", "file"):
            seat["photos"] += 1

        outcome = event["outcome"]
        if outcome:
            outcomes[outcome] += 1
            seat["trouble"][outcome] += 1
            if outcome in REFUSALS:
                seat["refused"] += 1
            else:
                seat["failed"] += 1

    devices: collections.Counter[tuple[str, str]] = collections.Counter()
    systems: dict[tuple[str, str], collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    owners: dict[tuple[str, str], collections.Counter[int]] = collections.defaultdict(
        collections.Counter
    )
    sent_as: collections.Counter[str] = collections.Counter()
    file_types: collections.Counter[str] = collections.Counter()
    verdicts: dict[str, collections.Counter[str]] = {
        axis: collections.Counter() for axis in ("originality", "privacy", "structure")
    }
    findings: collections.Counter[str] = collections.Counter()
    countries: collections.Counter[str] = collections.Counter()
    places: collections.Counter[str] = collections.Counter()
    camera_clocks: collections.Counter[str] = collections.Counter()
    states: collections.Counter[str] = collections.Counter()
    editors: collections.Counter[str] = collections.Counter()
    durations: list[int] = []
    sizes: list[int] = []
    ages: list[int] = []
    tag_counts: list[int] = []
    stripped_on_arrival = 0
    fully_stripped = 0
    analysed = 0
    refused_here = 0
    carried = collections.Counter({"gps": 0, "serial": 0, "stripped": 0, "edited": 0})
    funnel = collections.Counter({"offered": 0, "tags": 0, "clean": 0, "backup": 0})
    from_events = 0
    with_capture = 0

    for line in ledger:
        seat = seat_for(int(line["user_id"]), line["at"])
        if line["outcome"]:
            refused_here += 1
            continue
        # Counted after the refusals, not before: a refusal has always lived on
        # `events` and always will, so counting it here would put the "read from
        # the older schema" note under every report the bot has ever produced.
        if line["source"] == "events":
            from_events += 1

        analysed += 1
        seat["analysed"] += 1
        if line["sent_as"]:
            sent_as[line["sent_as"]] += 1
        if line["file_type"]:
            file_types[line["file_type"]] += 1

        make, model = (line["make"] or "").strip(), (line["model"] or "").strip()
        if make or model:
            key = (make, model)
            devices[key] += 1
            seat["devices"][key] += 1
            owners[key][int(line["user_id"])] += 1
            if line["os"]:
                systems[key][str(line["os"]).strip()] += 1
        else:
            stripped_on_arrival += 1
            if line["stripped"]:
                fully_stripped += 1

        for axis, counter in verdicts.items():
            if line[axis]:
                counter[str(line[axis])] += 1
        for finding in line["findings"]:
            findings[finding] += 1
        if line["editor"]:
            editors[str(line["editor"])] += 1
            carried["edited"] += 1
        if line["had_gps"]:
            carried["gps"] += 1
            seat["gps"] += 1
        if line["has_serial"]:
            carried["serial"] += 1
        if line["stripped"]:
            carried["stripped"] += 1

        funnel["offered"] += int(bool(line["clean_offered"]))
        funnel["tags"] += line["tags_taken"]
        funnel["clean"] += line["clean_taken"]
        funnel["backup"] += line["backup_taken"]

        if line["duration_ms"]:
            durations.append(int(line["duration_ms"]))
        if line["size_bytes"]:
            sizes.append(int(line["size_bytes"]))
        if line["tag_count"] is not None:
            tag_counts.append(int(line["tag_count"]))
        if line["age_days"] is not None:
            ages.append(int(line["age_days"]))
        if line["taken_date"] or line["country"] or line["taken_offset"]:
            with_capture += 1
        if line["country"]:
            countries[str(line["country"])] += 1
        if line["place"]:
            places[str(line["place"])] += 1
        if line["taken_offset"]:
            camera_clocks[str(line["taken_offset"])] += 1
        if line["state"]:
            states[str(line["state"])] += 1
            if line["state"] == "stored":
                seat["kept"] += 1
                seat["bytes_kept"] += int(line["bytes_kept"] or 0)

    roster = []
    for user_id, seat in per_user.items():
        known = people.get(user_id, {})
        zone = guess_zone(seat["hours"])
        roster.append(
            {
                "user_id": user_id,
                "username": _text(known.get("username")),
                "name": safe(
                    " ".join(
                        part for part in (known.get("first_name"), known.get("last_name")) if part
                    ),
                    EXPORT_LIMIT,
                )
                or None,
                "client_language": _text(known.get("language_code")),
                "chosen_language": _text(chosen.get(user_id)),
                "is_premium": bool(known.get("is_premium")),
                "is_bot": bool(known.get("is_bot")),
                # Two different questions, and the old report answered the
                # second with the first: `people` records when the bot ever met
                # them, which under --since or --user is a date outside the
                # table it was printed in.
                "first_seen": known.get("first_seen") or seat["first"],
                "last_seen": known.get("last_seen") or seat["last"],
                "first_here": seat["first"],
                "last_here": seat["last"],
                "events": seat["events"],
                "photos": seat["photos"],
                "analysed": seat["analysed"],
                "refused": seat["refused"],
                "failed": seat["failed"],
                "trouble": dict(seat["trouble"].most_common()),
                "with_gps": seat["gps"],
                "kept": seat["kept"],
                "bytes_kept": seat["bytes_kept"],
                "active_days": len(seat["days"]),
                "zone": zone,
                "utc_offset_guess": zone["offset"] if zone else None,
                "hours": dict(seat["hours"]),
                "devices": [
                    {"make": make, "model": model, "photos": count}
                    for (make, model), count in seat["devices"].most_common()
                ],
            }
        )
    # Sorted by pictures, not by interactions. Interactions count /start and
    # every button press under a report, so the busiest account by that measure
    # is whoever pressed the most buttons, not whoever sent the most photos.
    roster.sort(key=lambda row: (-row["photos"], -row["events"], row["user_id"]))

    names = {row["user_id"]: _label(row) for row in roster}
    for line in ledger:
        line["who"] = names.get(int(line["user_id"]), str(line["user_id"]))

    known_total = len(people)
    active = len(per_user)
    # With no window there is nothing for "new" to be new *since*, so the split
    # is undefined rather than universal. It used to report every account as
    # new and none as returning, which is the opposite of what --all shows.
    new = sum(1 for row in roster if (row["first_seen"] or "") >= since) if since else None

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "archive_dir": settings.get("archive_dir"),
        "window": {"since": since, "until": picks.until or now.isoformat(timespec="seconds")},
        "filters": picks.spoken(),
        "schema": {
            "photos_table": "photos" in tables,
            "from_events": from_events,
        },
        "people": {
            "known": known_total,
            "active": active,
            "new": new,
            "returning": (active - new) if new is not None else None,
        },
        "use": {
            "interactions": len(events),
            "photos": kinds.get("photo", 0) + kinds.get("file", 0),
            "analysed": analysed,
            "refused": refused_here,
            "stripped_on_arrival": stripped_on_arrival,
            # No camera *and* no timestamp: a file that had been through a
            # pipeline before it ever reached the bot.
            "fully_stripped": fully_stripped,
        },
        "photos_log": ledger,
        "roster": roster,
        "days": dict(sorted(days.items())),
        "hours": {hour: hours.get(hour, 0) for hour in range(24)},
        "kinds": dict(kinds.most_common()),
        "actions": dict(actions.most_common()),
        "outcomes": dict(outcomes.most_common()),
        "chats": dict(chats.most_common()),
        "client_languages": dict(
            collections.Counter(row["client_language"] or "unknown" for row in roster).most_common()
        ),
        "devices": [
            {
                "make": make,
                "model": model,
                "photos": count,
                "os": dict(systems[(make, model)].most_common()),
                "owners": [
                    {"user_id": who, "photos": many, "name": names.get(who, str(who))}
                    for who, many in owners[(make, model)].most_common()
                ],
            }
            for (make, model), count in devices.most_common()
        ],
        "sent_as": dict(sent_as.most_common()),
        "file_types": dict(file_types.most_common()),
        "pictures": {
            "verdicts": {axis: dict(counter.most_common()) for axis, counter in verdicts.items()},
            "findings": dict(findings.most_common()),
            "editors": dict(editors.most_common()),
            "carried": dict(carried),
            "funnel": dict(funnel),
            "duration_ms": _spread(durations),
            "size_bytes": _spread(sizes),
            "tag_count": _spread(tag_counts),
        },
        "capture": {
            "countries": dict(countries.most_common()),
            "places": dict(places.most_common()),
            "camera_clocks": dict(camera_clocks.most_common()),
            "age_days": _spread(ages),
            "with_capture": with_capture,
            "without_capture": analysed - with_capture,
        },
        "archive": _archive(ledger, states),
    }


def _label(row: dict[str, Any]) -> str:
    """The shortest thing that identifies an account to a human."""
    return row["name"] or (f"@{row['username']}" if row["username"] else str(row["user_id"]))


def _spread(values: list[int]) -> dict[str, Any] | None:
    """Median and worst case, which is all a summary line has room for."""
    if not values:
        return None
    return {
        "count": len(values),
        "median": round(statistics.median(values)),
        "max": max(values),
        "total": sum(values),
    }


def _archive(ledger: list[dict[str, Any]], states: collections.Counter[str]) -> dict[str, Any]:
    """What the archive kept, or refused to.

    Distinct bytes rather than the sum of the column: a duplicate row carries a
    full byte count while costing nothing on disk, so summing it would report a
    disk that is fuller than it is. The saving is the difference, which is the
    only honest way to say what deduplication bought.
    """
    kept = [line for line in ledger if line["state"]]
    if not kept:
        return {"present": False}
    blobs: dict[str, int] = {}
    claimed = 0
    for line in kept:
        if line["state"] in ("stored", "duplicate"):
            claimed += int(line["bytes_kept"] or 0)
        if line["state"] == "stored" and line["sha256"]:
            blobs[line["sha256"]] = max(blobs.get(line["sha256"], 0), int(line["bytes_kept"] or 0))
    stamps = sorted(line["at"] for line in kept if line["state"] == "stored")
    return {
        "present": True,
        "rows": len(kept),
        "states": dict(states.most_common()),
        "distinct": len(blobs),
        "bytes_held": sum(blobs.values()),
        "bytes_saved": max(0, claimed - sum(blobs.values())),
        "oldest": stamps[0][:10] if stamps else None,
        "newest": stamps[-1][:10] if stamps else None,
    }


# ------------------------------------------------------------------ rendering


def ledger_columns(width: int) -> dict[str, int]:
    """Widths for the picture ledger, laid out for 80 and grown from there.

    The surplus goes to the three columns a stranger's photograph fills —
    the name, the camera and the findings — because those are what get clipped
    at 80 and the rest never do.
    """
    extra = max(0, width - NARROW)
    who, camera, system = 25 * extra // 100, 30 * extra // 100, 15 * extra // 100
    return {
        "when": 11,
        "who": 12 + who,
        # Seventeen fits "samsung SM-S918B" whole; ten fits "One UI 6.1", which
        # is on every Samsung. Both were a character short and clipped the one
        # part of the string that identified the phone.
        "camera": 17 + camera,
        "os": 10 + system,
        "sent": 5,
        "type": 4,
        # Last column, so it takes the rounding as well as its share.
        "found": 13 + extra - who - camera - system,
    }


def what_it_carried(line: dict[str, Any]) -> str:
    """The findings on one picture, in the width of a table cell.

    Ordered by what an admin acts on: a coordinate first, then a serial number
    that ties two photographs to one camera body, then evidence of editing, then
    the worst verdict. An empty cell is ambiguous on purpose only for rows out
    of the old schema, and the note under the table says which those are.
    """
    marks = []
    if line["had_gps"]:
        marks.append("GPS")
    if line["has_serial"]:
        marks.append("serial")
    if line["editor"] or line["originality"] in ("poor", "bad"):
        marks.append("edited")
    worst = max(
        (str(line[axis]) for axis in ("originality", "privacy", "structure") if line[axis]),
        key=lambda level: LEVEL_RANK.get(level, -1),
        default="",
    )
    if LEVEL_RANK.get(worst, 0) >= 2:
        marks.append("!" + worst)
    if not marks:
        marks.append("stripped" if line["stripped"] else "—")
    return " ".join(marks)


def render_ledger(stats: dict[str, Any], ink: Ink, *, width: int, limit: int) -> None:
    """One line per picture, oldest first, with the refusals in place.

    This is the section the whole report was missing. Every other table is a
    projection of these rows, and until they were printed there was no way to
    put a person, a camera, an hour and a finding back on the same line.
    """
    log = stats["photos_log"]
    size = ledger_columns(width)
    if len({line["user_id"] for line in log}) == 1:
        # A drill-down repeats one name down the whole page. The width is worth
        # more to the camera and the findings, which are what differ per row.
        size["camera"] += size["who"] // 2 + 1
        size["found"] += size["who"] - size["who"] // 2
        size["who"] = -1
    turned_away = sum(1 for line in log if line["outcome"])
    shown = log[-limit:] if limit and len(log) > limit else log

    out = sys.stdout.write
    out(
        "\n"
        + ink.bold("WHAT WAS SENT")
        + ink.dim(
            f"  {len(log) - turned_away} analysed · {turned_away} turned away"
            f" · {len(shown)} shown"
        )
        + "\n"
    )
    header = (
        f"  {pad('when (UTC)', size['when'] + 1)}"
        f"{pad('who', size['who'] + 1) if size['who'] >= 0 else ''}"
        f"{pad('camera', size['camera'] + 1)}{pad('os', size['os'] + 1)}"
        f"{pad('sent', size['sent'] + 1)}{pad('type', size['type'] + 1)}found"
    )
    out(ink.dim(header) + "\n")
    out(ink.dim("  " + "─" * min(width - 2, len(header) - 2)) + "\n")

    tail = width - 2 - (size["when"] + 1) - (size["who"] + 1 if size["who"] >= 0 else 0)
    for line in shown:
        stamp = pad(line["at"][5:16].replace("T", " "), size["when"] + 1)
        who = cell(line["who"], size["who"]) + " " if size["who"] >= 0 else ""
        if line["outcome"]:
            why = REFUSALS.get(line["outcome"]) or FAILURES.get(line["outcome"], "not served")
            out(f"  {stamp}{who}" + ink.yellow(clip(f"· {line['outcome']} — {why}", tail)) + "\n")
            continue
        camera = " ".join(part for part in (line["make"], line["model"]) if part)
        out(
            f"  {stamp}{who}"
            f"{cell(camera or '—', size['camera'])} "
            f"{cell(line['os'] or '—', size['os'])} "
            f"{cell(line['sent_as'] or '—', size['sent'])} "
            f"{cell(line['file_type'] or '—', size['type'])} "
            f"{clip(what_it_carried(line), size['found'])}\n"
        )

    if len(shown) < len(log):
        out(ink.dim(f"  … {len(log) - len(shown)} earlier not shown — --limit 0 for all\n"))
    if stats["schema"]["from_events"]:
        out(
            ink.dim(
                f"  {stats['schema']['from_events']} of these were read from the older"
                " interaction log,\n  which kept only the camera and whether the file was"
                " stripped — a blank\n  column on those is 'never recorded', not 'the photo"
                " had none'.\n"
            )
        )


def render_who(stats: dict[str, Any], ink: Ink, *, width: int, limit: int) -> None:
    """The accounts, ordered by pictures, each with the camera it usually sends.

    The camera belongs here. It was in DEVICES only, as a global counter keyed
    on the model, so an admin could read that somebody sent thirty photographs
    and separately that thirty photographs came from an iPhone, and never join
    the two. First seen moved to --user: it is a fact about an account that is
    read once, and the column it occupied is worth more to the camera.
    """
    extra = max(0, width - NARROW)
    name_w, user_w = 12 + 30 * extra // 100, 12 + 25 * extra // 100
    camera_w = 18 + extra - (30 * extra // 100) - (25 * extra // 100)

    out = sys.stdout.write
    out("\n" + ink.bold("WHO") + "\n")
    out(
        ink.dim(
            f"  {pad('id', 12)}{pad('name', name_w + 1)}{pad('username', user_w + 1)}"
            f"{pad('lang', 6)}{pad('camera', camera_w + 1)}{'pics':>5} {'last':>5}"
        )
        + "\n"
    )
    shown = stats["roster"][:limit] if limit else stats["roster"]
    for row in shown:
        language = row["client_language"] or "—"
        if row["chosen_language"] and row["chosen_language"] != row["client_language"]:
            language = f"{language}→{row['chosen_language']}"
        cameras = row["devices"]
        if cameras:
            top = " ".join(part for part in (cameras[0]["make"], cameras[0]["model"]) if part)
            # The +N is the truncation made visible: two phones in one cell
            # would otherwise read as one, and this table has room for one.
            camera = f"{top} +{len(cameras) - 1}" if len(cameras) > 1 else top
        else:
            camera = "—"
        out(
            f"  {cell(str(row['user_id']), 11)} "
            f"{cell(row['name'] or '—', name_w)} "
            f"{cell('@' + row['username'] if row['username'] else '—', user_w)} "
            f"{cell(language, 5)} "
            f"{cell(camera, camera_w)} "
            f"{row['photos']:>4} {(row['last_here'] or '')[5:10]:>5}"
            + (ink.yellow("★") if row["is_premium"] else "")
            + "\n"
        )
    if limit and len(stats["roster"]) > limit:
        rest = len(stats["roster"]) - limit
        out(ink.dim(f"  … and {rest} more — pass --limit 0 to list everyone\n"))
    out(ink.dim("  ★ Telegram Premium · pics counts what they sent, refusals included\n"))


def render_devices(stats: dict[str, Any], ink: Ink, *, width: int, limit: int) -> None:
    """Cameras, and who sends from them.

    Owners are the point of the rewrite. This was a global counter keyed on
    (make, model), so two people carrying the same phone had their OS versions
    merged into one cell and there was no way to tell whose was whose.
    """
    out = sys.stdout.write
    out(
        "\n"
        + ink.bold("DEVICES")
        + ink.dim("  (read out of the photos, not from Telegram)")
        + "\n"
    )
    owner_w = max(18, width - 58)
    if stats["devices"]:
        out(
            ink.dim(f"  {pad('camera', 25)}{'pics':>4}  {pad('os', 20)}  who")
            + "\n"
        )
        for entry in stats["devices"][: limit or None]:
            label = " ".join(part for part in (entry["make"], entry["model"]) if part)
            systems = ", ".join(
                f"{name} ({count})" if count > 1 else name for name, count in entry["os"].items()
            )
            who = ", ".join(f"{o['name']} ({o['photos']})" for o in entry["owners"])
            out(
                f"  {cell(label, 24)} {entry['photos']:>4}  "
                f"{cell(systems or '—', 20)}  {clip(safe(who), owner_w)}\n"
            )
    else:
        out(ink.dim("  nothing — every picture arrived with its camera already removed\n"))
    if stats["use"]["stripped_on_arrival"]:
        out(
            f"  {cell('(no camera in the file)', 24)} "
            f"{stats['use']['stripped_on_arrival']:>4}"
            + ink.dim("  stripped before it reached the bot\n")
        )
    routes = " · ".join(
        f"{'as a file' if how == 'file' else 'as a photo'} {count}"
        for how, count in stats["sent_as"].items()
    )
    if routes:
        out(f"\n  how they sent it  {routes}\n")
    if stats["file_types"]:
        out(f"  file types        {tally(stats['file_types'])}\n")


def render_pictures(stats: dict[str, Any], ink: Ink, *, width: int) -> None:
    """What the photographs were, as opposed to who sent them."""
    facts = stats["pictures"]
    if not stats["use"]["analysed"]:
        return
    out = sys.stdout.write
    out("\n" + ink.bold("WHAT THE PICTURES WERE") + "\n")
    for axis in ("originality", "privacy", "structure"):
        levels = facts["verdicts"][axis]
        if levels:
            out(f"  {pad(axis, 17)} {clip(tally(levels), width - 21)}\n")
    if facts["findings"]:
        top = dict(list(facts["findings"].items())[:4])
        out(f"  {pad('most found', 17)} {clip(tally(top), width - 21)}\n")
    carried = facts["carried"]
    if any(carried.values()):
        out(
            f"  {pad('carried', 17)} {carried['gps']} with GPS · "
            f"{carried['serial']} with a serial · {carried['stripped']} already stripped\n"
        )
    funnel = facts["funnel"]
    if funnel["offered"] or funnel["tags"]:
        out(
            f"  {pad('buttons pressed', 17)} clean offered on {funnel['offered']} → "
            f"tags {funnel['tags']} · clean {funnel['clean']} · backup {funnel['backup']}\n"
        )
    spread = facts["duration_ms"]
    if spread:
        out(
            f"  {pad('analysis took', 17)} median {took(spread['median'])}"
            f" · slowest {took(spread['max'])}\n"
        )
    spread = facts["size_bytes"]
    if spread:
        out(
            f"  {pad('file size', 17)} median {sized(spread['median'])}"
            f" · largest {sized(spread['max'])}\n"
        )
    spread = facts["tag_count"]
    if spread:
        out(f"  {pad('tags per photo', 17)} median {spread['median']} · most {spread['max']}\n")


def render_where(stats: dict[str, Any], ink: Ink, *, width: int, limit: int) -> None:
    """The three weak proxies for a location, each labelled with its quality."""
    out = sys.stdout.write
    out("\n" + ink.bold("WHERE") + ink.dim("  (Telegram gives a bot no location at all)") + "\n")
    out(f"  client language   {clip(tally(stats['client_languages']) or '—', width - 22)}\n")
    if stats["chats"]:
        out(f"  chats             {clip(tally(stats['chats']), width - 22)}\n")

    out("\n  " + ink.dim("waking hours, guessed from when each person writes:") + "\n")
    for row in (stats["roster"][:limit] if limit else stats["roster"]):
        zone = row["zone"]
        if zone:
            # The width is the answer, not a footnote to it: the old report
            # printed a bare "UTC+2 2" for everyone at once and "unknown" for
            # anybody under fifteen messages, which was most accounts.
            guess = f"awake ≈ UTC{zone['offset']:+d} ±{zone['width']}h"
            evidence = (
                f"{zone['messages']} msgs, {zone['first_hour']:02d}–{zone['last_hour']:02d} UTC"
            )
        else:
            guess, evidence = "no answer", f"{row['events']} msgs, too few to average"
        out(f"    {cell(_label(row), 18)} {pad(guess, 22)}{ink.dim(evidence)}\n")


def render_capture(stats: dict[str, Any], ink: Ink, *, width: int) -> None:
    """Where the photographs were taken, which is a different claim entirely.

    Kept apart from the activity-hours guess on purpose. This is read out of a
    photograph's own GPS tags and is a fact about the picture; that is an
    inference about a person from the clock. Printing them together would let
    the strong one lend credibility to the weak one.
    """
    capture = stats["capture"]
    if not capture["with_capture"]:
        return
    out = sys.stdout.write
    out(
        "\n"
        + ink.bold("WHERE THE PICTURES WERE TAKEN")
        + ink.dim("  (from the photo's own tags)")
        + "\n"
    )
    if capture["countries"]:
        out(f"  countries         {clip(tally(capture['countries']), width - 22)}\n")
    if capture["places"]:
        top = dict(list(capture["places"].items())[:3])
        out(f"  places            {clip(tally(top), width - 22)}\n")
    if capture["camera_clocks"]:
        out(f"  camera clock      {clip(tally(capture['camera_clocks']), width - 22)}\n")
    ages = capture["age_days"]
    if ages:
        out(f"  age when sent     median {ages['median']} days · oldest {ages['max']} days\n")
    out(
        ink.dim(
            f"  {capture['without_capture']} of {stats['use']['analysed']} carried"
            " no capture data at all\n"
        )
    )


def render_when(stats: dict[str, Any], ink: Ink, *, limit: int) -> None:
    out = sys.stdout.write
    out("\n" + ink.bold("WHEN") + "\n")
    days = stats["days"]
    if days:
        peak = max(days.values())
        recent = list(days.items())[-21:]
        for day, count in recent:
            out(f"  {day}  {ink.cyan(pad(bar(count, peak), 27))}{count:>5}\n")
        if len(days) > len(recent):
            out(ink.dim(f"  … {len(days) - len(recent)} earlier days not shown\n"))
    hours = [stats["hours"][hour] for hour in range(24)]
    if any(hours):
        busiest = max(range(24), key=lambda hour: hours[hour])
        out(f"\n  hour of day  {ink.cyan(sparkline(hours))}\n")
        out(ink.dim(f"               {hour_axis()}\n"))
        out(ink.dim(f"               busiest {busiest:02d}:00 UTC ({hours[busiest]})\n"))
    out("\n" + ink.bold("WHAT THEY DID") + "\n")
    for name, count in stats["kinds"].items():
        out(f"  {pad(name, 12)}{count:>6}\n")
    top = list(stats["actions"].items())[: limit or None]
    if top:
        out(ink.dim("  " + " · ".join(f"{name} {count}" for name, count in top) + "\n"))


def render_trouble(stats: dict[str, Any], ink: Ink, *, width: int) -> None:
    """Refusals and failures, against the accounts they happened to.

    A bare count told the operator that three uploads were refused and nothing
    about whether that was three people hitting the quota once or one account
    hammering it, which are opposite problems with opposite answers.
    """
    if not stats["outcomes"]:
        return
    out = sys.stdout.write
    out("\n" + ink.bold("TURNED AWAY OR BROKEN") + "\n")
    for name, count in stats["outcomes"].items():
        why = REFUSALS.get(name) or FAILURES.get(name, "not served")
        blamed = [(row, row["trouble"][name]) for row in stats["roster"] if name in row["trouble"]]
        blamed.sort(key=lambda pair: -pair[1])
        who = ", ".join(f"{_label(row)} ({many})" for row, many in blamed[:4])
        out(
            f"  {cell(name, 14)}{count:>4}  {cell(why, 26)}  "
            f"{clip(safe(who) or '—', max(12, width - 50))}\n"
        )
    broken = sum(1 for line in stats["photos_log"] if line["errors"])
    noisy = sum(1 for line in stats["photos_log"] if line["warnings"])
    if broken or noisy:
        out(
            ink.dim(
                f"  {broken} analyses hit an error · {noisy} warned and reported anyway\n"
            )
        )


def render_archive(stats: dict[str, Any], ink: Ink, *, width: int, root: Path | None) -> None:
    """What was kept on disk, and where, so a path can be pasted into scp."""
    archive = stats["archive"]
    if not archive["present"]:
        return
    out = sys.stdout.write
    out(
        "\n"
        + ink.bold("ARCHIVE")
        + ink.dim(
            "  " + clip(str(root), width - 9)
            if root
            else "  (pass --archive DIR to check the files)"
        )
        + "\n"
    )
    states = archive["states"]
    # Named apart from the headline on purpose: "pictures sent" counts every
    # upload including the ones that were refused, so it will never equal the
    # number of files on disk, and two counts that disagree without saying why
    # read as a bug.
    said = [f"{stats['use']['photos']} sent", f"{states.get('stored', 0)} kept"]
    said += [
        f"{count} {ARCHIVE_STATES.get(name, name)}"
        for name, count in states.items()
        if name != "stored"
    ]
    out("  " + " · ".join(said) + "\n")
    out(
        f"  holding {sized(archive['bytes_held'])} in {archive['distinct']} distinct files"
        f" · {sized(archive['bytes_saved'])} saved by deduplication\n"
    )
    if archive["oldest"]:
        out(f"  oldest {archive['oldest']} · newest {archive['newest']}\n")

    path_w = max(20, width - 36)
    kept = [line for line in stats["photos_log"] if line["rel_path"]]
    for line in kept[-8:]:
        here = _under(root, line["rel_path"]) if root else None
        if not root:
            missing = ""
        elif here is None:
            missing = " (not under the root)"
        else:
            missing = "" if here.exists() else " (gone)"
        out(
            f"  {line['at'][:10]} {cell(line['who'], 12)} "
            f"{cell(line['rel_path'] + missing, path_w)} {sized(line['bytes_kept']):>9}\n"
        )
    if len(kept) > 8:
        out(ink.dim(f"  … {len(kept) - 8} more kept files — --csv-photos lists them all\n"))


def _under(root: Path, relative: str) -> Path | None:
    """Resolve an archived path against the root, refusing to leave it.

    The bot writes this column, so a traversal here would be a bug rather than
    an attack — but this script is pointed at databases pulled off servers, and
    a report that stats /etc on the strength of a text column is not one.
    """
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return root / candidate


def render(
    stats: dict[str, Any],
    ink: Ink,
    *,
    source: str,
    limit: int,
    width: int = NARROW,
    archive_root: Path | None = None,
) -> None:
    """The whole report, ledger first.

    Order is the argument this rewrite makes. The old report opened with WHO,
    which is a summary, and never printed the rows it summarised — so the one
    question an admin actually asks, "who sent that photograph and what was in
    it", had no answer anywhere on the page.
    """
    out = sys.stdout.write
    window = stats["window"]["since"]
    span = f"since {window[:10]}" if window else "all time"

    out(ink.bold("findpic bot · who used it") + "\n")
    out(ink.dim("database  " + clip(source, width - 10)) + "\n")
    out(ink.dim(f"window    {span} · generated {stats['generated_at'][:16]} · all times UTC") + "\n")
    if stats["filters"]:
        # Loud rather than dim: a filtered report that looks like a full one is
        # how an operator concludes the bot is quiet when it is not.
        out(ink.yellow(f"filters   {clip(' · '.join(stats['filters']), width - 10)}") + "\n")

    people, use = stats["people"], stats["use"]
    out("\n")
    if people["new"] is None:
        out(
            f"  {ink.bold(str(people['known']))} accounts known · "
            f"{people['active']} of them active, over the whole history\n"
        )
    else:
        out(
            f"  {ink.bold(str(people['known']))} accounts known · "
            f"{people['active']} active in this window · "
            f"{people['new']} new · {people['returning']} returning\n"
        )
    out(
        f"  {ink.bold(str(use['interactions']))} interactions · "
        f"{use['photos']} pictures sent · {use['analysed']} analysed"
    )
    if use["refused"]:
        out(f" · {use['refused']} turned away")
    out("\n")
    if use["stripped_on_arrival"]:
        out(ink.dim(f"  {use['stripped_on_arrival']} arrived with no camera in them\n"))

    if not stats["roster"]:
        out("\n" + ink.yellow("Nothing recorded in this window.") + "\n")
        return

    render_ledger(stats, ink, width=width, limit=limit)
    render_who(stats, ink, width=width, limit=limit)
    render_devices(stats, ink, width=width, limit=limit)
    render_pictures(stats, ink, width=width)
    render_where(stats, ink, width=width, limit=limit)
    render_capture(stats, ink, width=width)
    render_when(stats, ink, limit=limit)
    render_trouble(stats, ink, width=width)
    render_archive(stats, ink, width=width, root=archive_root)


def render_user(stats: dict[str, Any], who: str, ink: Ink, *, width: int = NARROW) -> int:
    """Everything on one account, including the pictures it sent."""
    name_wanted = str(who).lstrip("@").lower()
    row = next(
        (
            entry
            for entry in stats["roster"]
            if str(entry["user_id"]) == str(who)
            or (entry["username"] or "").lower() == name_wanted
        ),
        None,
    )
    if row is None:
        print(f"no activity recorded for {safe(str(who), 32)} in this window", file=sys.stderr)
        return 1

    out = sys.stdout.write
    name = safe(row["name"], 40) or "—"
    username = f"@{safe(row['username'], 32)}" if row["username"] else "no username"
    out(ink.bold(f"{name}  {username}  ({row['user_id']})") + "\n\n")
    out(f"  first seen     {row['first_seen'][:16].replace('T', ' ')}")
    out(ink.dim("   the first time the bot ever saw them\n"))
    out(f"  in this window {row['first_here'][:10]} to {row['last_here'][:10]}\n")
    out(f"  active on      {row['active_days']} days\n")
    out(f"  interactions   {row['events']}\n")
    out(f"  pictures       {row['photos']} sent · {row['analysed']} analysed")
    out(f" · {row['with_gps']} with GPS\n" if row["with_gps"] else "\n")
    if row["refused"] or row["failed"]:
        out(f"  turned away    {row['refused']} refused · {row['failed']} failed")
        out(ink.dim(f"   {tally(row['trouble'])}\n"))
    out(f"  client asks in {row['client_language'] or '—'}")
    out(f" · chose {row['chosen_language']}\n" if row["chosen_language"] else "\n")
    out(f"  premium        {'yes' if row['is_premium'] else 'no'}\n")
    if row["kept"]:
        out(f"  archived       {row['kept']} files · {sized(row['bytes_kept'])}\n")

    zone = row["zone"]
    if zone:
        out(f"  awake around   UTC{zone['offset']:+d} ±{zone['width']}h")
        out(
            ink.dim(
                f"   from {zone['messages']} messages between"
                f" {zone['first_hour']:02d}:00 and {zone['last_hour']:02d}:00 UTC\n"
            )
        )
    else:
        out("  awake around   " + ink.dim("not enough activity to average\n"))

    hours = [row["hours"].get(str(h), row["hours"].get(h, 0)) for h in range(24)]
    out(f"\n  hour of day  {ink.cyan(sparkline(hours))}\n")
    out(ink.dim(f"               {hour_axis()}\n"))
    if row["devices"]:
        out("\n" + ink.bold("  cameras in their photos") + "\n")
        for device in row["devices"]:
            label = " ".join(part for part in (device["make"], device["model"]) if part)
            out(f"    {cell(label, 40)}{device['photos']:>5}\n")

    mine = [line for line in stats["photos_log"] if int(line["user_id"]) == row["user_id"]]
    if mine:
        render_ledger({**stats, "photos_log": mine}, ink, width=width, limit=20)
    return 0


# ------------------------------------------------------------------- exporting

ROSTER_COLUMNS = (
    "user_id",
    "username",
    "name",
    "client_language",
    "chosen_language",
    "is_premium",
    "first_seen",
    "last_seen",
    "active_days",
    "events",
    "photos",
    "analysed",
    "refused",
    "failed",
    "utc_offset_guess",
)

#: One row per picture. Ordered so the first screenful of a spreadsheet answers
#: the question the report exists for — who sent what, when, from what.
PHOTO_COLUMNS = (
    "at",
    "user_id",
    "who",
    "sent_as",
    "file_type",
    "make",
    "model",
    "os",
    "lens",
    "editor",
    "outcome",
    "originality",
    "privacy",
    "structure",
    "tag_count",
    "had_gps",
    "has_serial",
    "stripped",
    "country",
    "place",
    "taken_date",
    "taken_offset",
    "age_days",
    "lat",
    "lon",
    "size_bytes",
    "width",
    "height",
    "mime_type",
    "claimed_name",
    "duration_ms",
    "warnings",
    "errors",
    "clean_offered",
    "tags_taken",
    "clean_taken",
    "backup_taken",
    "state",
    "bytes_kept",
    "sha256",
    "rel_path",
    "chat_type",
    "findings",
    "source",
    "event_id",
    "photo_id",
)


def write_csv(stats: dict[str, Any], path: Path) -> None:
    """The roster, one row per account."""
    _write(path, ROSTER_COLUMNS, stats["roster"])


def write_photo_csv(stats: dict[str, Any], path: Path) -> None:
    """The ledger, one row per picture — the export the report never had."""
    _write(path, PHOTO_COLUMNS, stats["photos_log"])


def _write(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(row.get(key)) for key in columns})


def _cell(value: object) -> object:
    """One CSV field, safe to open in a spreadsheet.

    Every other output path in this script sanitises — safe() for the terminal —
    and this one did not. Two problems, both from text a stranger chose: control
    bytes and escape sequences survive into the file, and a value beginning
    ``=``, ``+``, ``-`` or ``@`` is executed as a formula by Excel and Sheets
    the moment the admin opens it.
    """
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    if not isinstance(value, str):
        return value
    text = safe(value)
    return "'" + text if text[:1] in "=+-@" else text


# ----------------------------------------------------------------------- main


def _env_port() -> int | None:
    """The ssh port from the environment, ignoring anything that is not one.

    A malformed value should fall back to ssh's own default rather than crash a
    report the operator is running to find out what is wrong.
    """
    raw = (os.environ.get("FINDPIC_BOT_PORT") or "").strip()
    return int(raw) if raw.isdigit() else None


def _date(value: str | None, flag: str) -> str | None:
    """A YYYY-MM-DD from the command line, or a refusal that names the flag."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise SystemExit(f"{flag} wants a date as YYYY-MM-DD, not {value!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot-stats.py",
        description="Who used the findpic Telegram bot, when, and with what.",
        epilog=(
            "Examples:\n"
            "  scripts/bot-stats.py --docker                    on the server itself\n"
            "  scripts/bot-stats.py --ssh you@server            from your laptop\n"
            "  scripts/bot-stats.py --ssh you@server --all      everything kept\n"
            "  scripts/bot-stats.py --db bot.sqlite3 --json     machine-readable\n"
            "  scripts/bot-stats.py --ssh you@server --user @someone\n"
            "  scripts/bot-stats.py --db bot.sqlite3 --photos --limit 0\n"
            "  scripts/bot-stats.py --db bot.sqlite3 --device iphone --with-gps\n"
            "\n"
            "Telegram tells a bot nothing about a device, an operating system or a\n"
            "location. Devices here are read from the photographs; the timezone is a\n"
            "guess from activity hours, printed with the width of the guess. Both are\n"
            "labelled in the report.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_argument_group("where the database is")
    source.add_argument("--db", type=Path, help="path to findpic-bot.sqlite3")
    source.add_argument(
        "--docker",
        action="store_true",
        help="read it out of the local Docker volume (needs the docker group, not root)",
    )
    source.add_argument(
        "--ssh",
        metavar="HOST",
        default=os.environ.get("FINDPIC_BOT_HOST"),
        help="the same, on another machine, over ssh (env: FINDPIC_BOT_HOST)",
    )
    source.add_argument(
        "--ssh-port",
        metavar="PORT",
        type=int,
        default=_env_port(),
        help="ssh port, when the server does not listen on 22 (env: FINDPIC_BOT_PORT)",
    )
    source.add_argument("--volume", default=DEFAULT_VOLUME, help="Docker volume holding the data")
    source.add_argument("--image", default=DEFAULT_IMAGE, help="image used to read the volume")
    source.add_argument(
        "--archive",
        type=Path,
        metavar="DIR",
        default=os.environ.get("ARCHIVE_DIR") or None,
        help="archive root, overriding what the bot recorded (env: ARCHIVE_DIR)",
    )

    narrow = parser.add_argument_group("what to count")
    narrow.add_argument("--days", type=int, default=30, metavar="N", help="window (default 30)")
    narrow.add_argument("--all", action="store_true", help="every record, ignoring --days")
    narrow.add_argument("--since", metavar="DATE", help="from this day, as YYYY-MM-DD")
    narrow.add_argument("--until", metavar="DATE", help="up to and including this day")
    narrow.add_argument(
        "--user",
        action="append",
        default=[],
        metavar="ID",
        help="one account, by id or @username. Repeatable",
    )
    narrow.add_argument("--device", metavar="SUBSTR", help="cameras matching this text")
    narrow.add_argument("--os", dest="system", metavar="SUBSTR", help="OS versions matching this")
    gps = narrow.add_mutually_exclusive_group()
    gps.add_argument(
        "--with-gps", dest="gps", action="store_true", default=None, help="only photos with GPS"
    )
    gps.add_argument("--no-gps", dest="gps", action="store_false", help="only photos without GPS")
    narrow.add_argument("--failed", action="store_true", help="only refusals and failures")
    narrow.add_argument("--sent-as", choices=("file", "photo"), help="how the picture arrived")
    narrow.add_argument(
        "--min-events", type=int, default=0, metavar="N", help="skip accounts quieter than this"
    )

    output = parser.add_argument_group("output")
    output.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    output.add_argument("--csv", type=Path, metavar="PATH", help="write the roster as CSV")
    output.add_argument(
        "--csv-photos", type=Path, metavar="PATH", help="write one row per picture as CSV"
    )
    output.add_argument("--photos", action="store_true", help="print the picture ledger and stop")
    output.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="rows per table, 0 for all (default 15, and all under --photos)",
    )
    output.add_argument("--no-color", action="store_true", help="disable styling")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ink = Ink(
        not args.no_color
        and not args.json
        and sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
    )
    width = terminal_width()

    now = dt.datetime.now(dt.timezone.utc)
    start = _date(args.since, "--since")
    if start:
        since: str | None = f"{start}T00:00:00+00:00"
    elif args.all or args.days <= 0:
        since = None
    else:
        since = (now - dt.timedelta(days=args.days)).isoformat(timespec="seconds")
    filters = Filters(
        until=_date(args.until, "--until"),
        users=tuple(args.user),
        device=args.device,
        system=args.system,
        gps=args.gps,
        failed=args.failed,
        sent_as=args.sent_as,
        min_events=max(0, args.min_events),
    )

    with tempfile.TemporaryDirectory(prefix="findpic-stats-") as scratch:
        workspace = Path(scratch)
        command = volume_command(args.volume, args.image)
        if args.docker:
            source = f"docker volume {args.volume}"
            database = pull(command, workspace, source, args.volume)
        elif args.ssh:
            source = f"{args.ssh}:{args.volume}"
            # ssh joins its arguments with spaces and hands the result to the
            # remote login shell, so the quoting has to survive that trip —
            # otherwise the `sh -c` payload comes apart and half of it runs on
            # the host instead of inside the container.
            ssh = ["ssh"]
            if args.ssh_port:
                ssh += ["-p", str(args.ssh_port)]
            database = pull([*ssh, args.ssh, shlex.join(command)], workspace, source, args.volume)
        else:
            live = args.db or find_database()
            source = str(live)
            database = snapshot(live, workspace)

        # Opened writable on purpose: both paths above hand back a copy in a
        # temporary directory, and SQLite needs write access to replay a
        # write-ahead log it did not close itself. The live file is never
        # opened at all.
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            stats = collect(conn, since, now, filters=filters)
        finally:
            conn.close()

    if args.csv:
        write_csv(stats, args.csv)
        print(f"wrote {args.csv} ({len(stats['roster'])} accounts)", file=sys.stderr)
    if args.csv_photos:
        write_photo_csv(stats, args.csv_photos)
        print(f"wrote {args.csv_photos} ({len(stats['photos_log'])} pictures)", file=sys.stderr)

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
        return 0
    if args.photos:
        # The ledger on its own is what somebody pipes into grep, so it runs to
        # full length unless a limit was asked for by name.
        render_ledger(stats, ink, width=width, limit=max(0, args.limit or 0))
        return 0
    limit = 15 if args.limit is None else max(0, args.limit)
    if len(args.user) == 1:
        return render_user(stats, args.user[0], ink, width=width)
    render(
        stats,
        ink,
        source=source,
        limit=limit,
        width=width,
        archive_root=args.archive or stats.get("archive_dir"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
