#!/usr/bin/env python3
"""Who used the findpic bot: accounts, activity, and the cameras they sent.

Point it at the bot's SQLite database — a local copy, or pulled straight out of
the server's Docker volume over ssh:

    scripts/bot-stats.py --db /data/findpic-bot.sqlite3
    scripts/bot-stats.py --ssh you@your.server
    scripts/bot-stats.py --ssh you@your.server --all --json > stats.json
    scripts/bot-stats.py --ssh you@your.server --user 5829771410

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
* **Where** has two weak proxies: the language the Telegram client asks in, and
  the hours of the day somebody is active, which puts their waking day somewhere
  on the clock. Neither is a location, and the second is worth an hour or two
  either way at best. Both are printed with the word "guess" on them.

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
import subprocess
import sys
import tarfile
import tempfile
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

BLOCKS = "▁▂▃▄▅▆▇█"

#: The local hour a person's messaging day balances around, used to turn an
#: average activity hour into a timezone. Mid-afternoon: late enough that the
#: morning and the evening both pull on it, early enough not to sit in the tail.
ACTIVITY_CENTRE = 15.0


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
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in (text or "")).strip()
    if limit and len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def pad(text: str, width: int) -> str:
    """Left-align in a fixed column, counting characters rather than bytes."""
    return text + " " * max(0, width - len(text))


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


# ------------------------------------------------------------------- gathering


def guess_offset(hours: collections.Counter[int], minimum: int = 15) -> int | None:
    """Somebody's UTC offset, guessed from the hours they are awake.

    Hours are a circle, so the average of one is a vector sum rather than an
    arithmetic mean — otherwise 23:00 and 01:00 average to noon. Taking that
    mean and calling it mid-afternoon local time gives an offset.

    The length of the resulting vector is how concentrated the activity is, and
    it doubles as the confidence check: somebody who writes at every hour of the
    day produces a vector near zero, and gets no answer rather than a made-up
    one. It is still only a guess — shift work, insomnia and travel all defeat
    it — but it is the one signal in this data that points at a part of the
    world at all, which is why it is printed with the word attached.
    """
    total = sum(hours.values())
    if total < minimum or len(hours) < 4:
        return None
    step = 2 * math.pi / 24
    x = sum(count * math.cos(hour * step) for hour, count in hours.items()) / total
    y = sum(count * math.sin(hour * step) for hour, count in hours.items()) / total
    if math.hypot(x, y) < 0.15:
        return None
    mean = (math.atan2(y, x) / step) % 24
    # Fold into the range the world actually uses: UTC-11 to UTC+12.
    return (round(ACTIVITY_CENTRE - mean) + 11) % 24 - 11


def collect(conn: sqlite3.Connection, since: str | None, now: dt.datetime) -> dict[str, Any]:
    """Everything the report needs, in one dictionary.

    The whole event table is pulled into memory and aggregated in Python. That
    would be wrong for a busy service; for a bot whose entire history is a few
    thousand rows it is far clearer than a dozen GROUP BY queries, and it lets
    the per-user timezone guess reuse the same rows.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "events" not in tables or "people" not in tables:
        raise SystemExit(
            "this database has no usage tables — it was written by a bot build from\n"
            "before they existed. Redeploy the bot and it will start recording.\n"
            "Until then all that is kept is the language preference in `users`."
        )

    where, params = ("WHERE at >= ?", (since,)) if since else ("", ())
    events = [dict(row) for row in conn.execute(f"SELECT * FROM events {where}", params)]
    people = {int(row["user_id"]): dict(row) for row in conn.execute("SELECT * FROM people")}
    chosen = {
        int(row["user_id"]): row["language"] for row in conn.execute("SELECT * FROM users")
    }

    per_user: dict[int, dict[str, Any]] = {}
    days: collections.Counter[str] = collections.Counter()
    hours: collections.Counter[int] = collections.Counter()
    kinds: collections.Counter[str] = collections.Counter()
    actions: collections.Counter[str] = collections.Counter()
    outcomes: collections.Counter[str] = collections.Counter()
    chats: collections.Counter[str] = collections.Counter()
    devices: collections.Counter[tuple[str, str]] = collections.Counter()
    systems: dict[tuple[str, str], collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    sent_as: collections.Counter[str] = collections.Counter()
    file_types: collections.Counter[str] = collections.Counter()
    stripped_on_arrival = 0
    fully_stripped = 0
    analysed = 0

    for event in events:
        user_id = int(event["user_id"])
        seat = per_user.setdefault(
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
                "first": event["at"],
                "last": event["at"],
            },
        )
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
            if outcome in REFUSALS:
                seat["refused"] += 1
            else:
                seat["failed"] += 1

        if event["sent_as"]:
            analysed += 1
            seat["analysed"] += 1
            sent_as[event["sent_as"]] += 1
            if event["stripped"]:
                fully_stripped += 1
            if event["file_type"]:
                file_types[event["file_type"]] += 1
            make, model = (event["make"] or "").strip(), (event["model"] or "").strip()
            if make or model:
                key = (make, model)
                devices[key] += 1
                seat["devices"][key] += 1
                if event["os"]:
                    systems[key][str(event["os"]).strip()] += 1
            else:
                stripped_on_arrival += 1

    roster = []
    for user_id, seat in per_user.items():
        known = people.get(user_id, {})
        roster.append(
            {
                "user_id": user_id,
                "username": known.get("username"),
                "name": " ".join(
                    part for part in (known.get("first_name"), known.get("last_name")) if part
                ),
                "client_language": known.get("language_code"),
                "chosen_language": chosen.get(user_id),
                "is_premium": bool(known.get("is_premium")),
                "is_bot": bool(known.get("is_bot")),
                "first_seen": known.get("first_seen") or seat["first"],
                "last_seen": known.get("last_seen") or seat["last"],
                "events": seat["events"],
                "photos": seat["photos"],
                "analysed": seat["analysed"],
                "refused": seat["refused"],
                "failed": seat["failed"],
                "active_days": len(seat["days"]),
                "utc_offset_guess": guess_offset(seat["hours"]),
                "hours": dict(seat["hours"]),
                "devices": [
                    {"make": make, "model": model, "photos": count}
                    for (make, model), count in seat["devices"].most_common()
                ],
            }
        )
    roster.sort(key=lambda row: (-row["events"], row["user_id"]))

    known_total = len(people)
    active = len(per_user)
    new = sum(1 for row in roster if not since or (row["first_seen"] or "") >= since)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {"since": since, "until": now.isoformat(timespec="seconds")},
        "people": {
            "known": known_total,
            "active": active,
            "new": new,
            "returning": active - new,
        },
        "use": {
            "interactions": len(events),
            "photos": kinds.get("photo", 0) + kinds.get("file", 0),
            "analysed": analysed,
            "stripped_on_arrival": stripped_on_arrival,
            # No camera *and* no timestamp: a file that had been through a
            # pipeline before it ever reached the bot.
            "fully_stripped": fully_stripped,
        },
        "roster": roster,
        "days": dict(sorted(days.items())),
        "hours": {hour: hours.get(hour, 0) for hour in range(24)},
        "kinds": dict(kinds.most_common()),
        "actions": dict(actions.most_common()),
        "outcomes": dict(outcomes.most_common()),
        "chats": dict(chats.most_common()),
        "client_languages": dict(
            collections.Counter(
                row["client_language"] or "unknown" for row in roster
            ).most_common()
        ),
        "offsets": dict(
            collections.Counter(
                "unknown" if row["utc_offset_guess"] is None else f"UTC{row['utc_offset_guess']:+d}"
                for row in roster
            ).most_common()
        ),
        "devices": [
            {
                "make": make,
                "model": model,
                "photos": count,
                "os": dict(systems[(make, model)].most_common()),
            }
            for (make, model), count in devices.most_common()
        ],
        "sent_as": dict(sent_as.most_common()),
        "file_types": dict(file_types.most_common()),
    }


# ------------------------------------------------------------------ rendering


def render(stats: dict[str, Any], ink: Ink, *, source: str, limit: int) -> None:
    out = sys.stdout.write
    window = stats["window"]["since"]
    span = f"since {window[:10]}" if window else "all time"

    out(ink.bold("findpic bot · who used it") + "\n")
    out(ink.dim(f"database  {source}") + "\n")
    out(ink.dim(f"window    {span} · generated {stats['generated_at'][:16]} · all times UTC") + "\n")

    people, use = stats["people"], stats["use"]
    out("\n")
    out(
        f"  {ink.bold(str(people['known']))} accounts known · "
        f"{people['active']} active in this window · "
        f"{people['new']} new · {people['returning']} returning\n"
    )
    out(
        f"  {ink.bold(str(use['interactions']))} interactions · "
        f"{use['photos']} pictures sent · {use['analysed']} analysed"
    )
    if use["stripped_on_arrival"]:
        out(ink.dim(f" · {use['stripped_on_arrival']} arrived with no camera in them"))
    out("\n")

    if not stats["roster"]:
        out("\n" + ink.yellow("Nothing recorded in this window.") + "\n")
        return

    # ------------------------------------------------------------------ who
    out("\n" + ink.bold("WHO") + "\n")
    header = (
        f"  {pad('id', 12)}{pad('name', 18)}{pad('username', 17)}"
        f"{pad('lang', 6)}{pad('first seen', 12)}{pad('last seen', 12)}"
        f"{'events':>7}{'photos':>8}"
    )
    out(ink.dim(header) + "\n")
    shown = stats["roster"][:limit] if limit else stats["roster"]
    for row in shown:
        username = f"@{row['username']}" if row["username"] else "—"
        language = row["client_language"] or "—"
        if row["chosen_language"] and row["chosen_language"] != row["client_language"]:
            language = f"{language}→{row['chosen_language']}"
        flags = " ★" if row["is_premium"] else ""
        out(
            f"  {pad(str(row['user_id']), 12)}"
            f"{pad(safe(row['name'], 17), 18)}"
            f"{pad(safe(username, 16), 17)}"
            f"{pad(language, 6)}"
            f"{pad((row['first_seen'] or '')[:10], 12)}"
            f"{pad((row['last_seen'] or '')[:10], 12)}"
            f"{row['events']:>7}{row['photos']:>8}{flags}\n"
        )
    if limit and len(stats["roster"]) > limit:
        rest = len(stats["roster"]) - limit
        out(ink.dim(f"  … and {rest} more — pass --limit 0 to list everyone\n"))
    out(ink.dim("  ★ Telegram Premium · lang shows client→chosen where they differ\n"))

    # ---------------------------------------------------------------- where
    out("\n" + ink.bold("WHERE") + ink.dim("  (Telegram gives a bot no location at all)") + "\n")
    languages = " · ".join(f"{code} {count}" for code, count in stats["client_languages"].items())
    out(f"  client language   {languages or '—'}\n")
    offsets = " · ".join(f"{zone} {count}" for zone, count in stats["offsets"].items())
    out(f"  waking hours      {offsets or '—'}")
    out(ink.dim("   guessed from when each person writes, ±1–2h\n"))
    chats = " · ".join(f"{kind} {count}" for kind, count in stats["chats"].items())
    if chats:
        out(f"  chats             {chats}\n")

    # ----------------------------------------------------------------- when
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

    # -------------------------------------------------------------- devices
    out(
        "\n"
        + ink.bold("DEVICES")
        + ink.dim("  (read out of the photos, not from Telegram)")
        + "\n"
    )
    if stats["devices"]:
        for entry in stats["devices"][: limit or None]:
            label = " ".join(part for part in (entry["make"], entry["model"]) if part)
            systems = ", ".join(
                f"{name} ({count})" if count > 1 else name for name, count in entry["os"].items()
            )
            out(f"  {pad(safe(label, 34), 35)}{entry['photos']:>5}  {ink.dim(safe(systems, 40))}\n")
    else:
        out(ink.dim("  nothing — every picture arrived with its camera already removed\n"))
    if stats["use"]["stripped_on_arrival"]:
        out(
            f"  {pad('(no camera in the file)', 35)}"
            f"{stats['use']['stripped_on_arrival']:>5}"
            + ink.dim("  stripped before it reached the bot\n")
        )
    routes = " · ".join(
        f"{'as a file' if how == 'file' else 'as a photo'} {count}"
        for how, count in stats["sent_as"].items()
    )
    if routes:
        out(f"\n  how they sent it  {routes}\n")
    suffixes = " · ".join(f"{name} {count}" for name, count in stats["file_types"].items())
    if suffixes:
        out(f"  file types        {suffixes}\n")

    # ----------------------------------------------------------------- what
    out("\n" + ink.bold("WHAT THEY DID") + "\n")
    for name, count in stats["kinds"].items():
        out(f"  {pad(name, 12)}{count:>6}\n")
    top = list(stats["actions"].items())[: limit or None]
    if top:
        out(ink.dim("  " + " · ".join(f"{name} {count}" for name, count in top) + "\n"))

    # ------------------------------------------------------------- refusals
    refused = {k: v for k, v in stats["outcomes"].items() if k in REFUSALS}
    failed = {k: v for k, v in stats["outcomes"].items() if k not in REFUSALS}
    if refused or failed:
        out("\n" + ink.bold("TURNED AWAY OR BROKEN") + "\n")
        for name, count in {**refused, **failed}.items():
            why = REFUSALS.get(name) or FAILURES.get(name, name)
            out(f"  {pad(name, 14)}{count:>5}  {ink.dim(why)}\n")


def render_user(stats: dict[str, Any], user_id: int, ink: Ink) -> int:
    """Everything on one account."""
    row = next((entry for entry in stats["roster"] if entry["user_id"] == user_id), None)
    if row is None:
        print(f"no activity recorded for {user_id} in this window", file=sys.stderr)
        return 1
    out = sys.stdout.write
    name = safe(row["name"]) or "—"
    username = f"@{safe(row['username'])}" if row["username"] else "no username"
    out(ink.bold(f"{name}  {username}  ({user_id})") + "\n\n")
    out(f"  first seen     {row['first_seen']}\n")
    out(f"  last seen      {row['last_seen']}\n")
    out(f"  active on      {row['active_days']} days\n")
    out(f"  interactions   {row['events']}\n")
    out(f"  pictures       {row['photos']} sent · {row['analysed']} analysed\n")
    if row["refused"] or row["failed"]:
        out(f"  turned away    {row['refused']} refused · {row['failed']} failed\n")
    out(f"  client asks in {row['client_language'] or '—'}")
    if row["chosen_language"]:
        out(f" · chose {row['chosen_language']}\n")
    else:
        out("\n")
    out(f"  premium        {'yes' if row['is_premium'] else 'no'}\n")
    offset = row["utc_offset_guess"]
    zone = f"UTC{offset:+d}" if offset is not None else "not enough activity to guess"
    out(f"  likely in      {zone}")
    out(ink.dim("   guessed from when they write, ±1–2h\n"))
    hours = [row["hours"].get(str(h), row["hours"].get(h, 0)) for h in range(24)]
    out(f"\n  hour of day  {ink.cyan(sparkline(hours))}\n")
    out(ink.dim(f"               {hour_axis()}\n"))
    if row["devices"]:
        out("\n" + ink.bold("  cameras in their photos") + "\n")
        for device in row["devices"]:
            label = " ".join(part for part in (device["make"], device["model"]) if part)
            out(f"    {pad(safe(label, 34), 35)}{device['photos']:>5}\n")
    return 0


def write_csv(stats: dict[str, Any], path: Path) -> None:
    columns = [
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
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in stats["roster"]:
            writer.writerow({key: row.get(key) for key in columns})


# ----------------------------------------------------------------------- main


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
            "  scripts/bot-stats.py --ssh you@server --user 12345678\n"
            "\n"
            "Telegram tells a bot nothing about a device, an operating system or a\n"
            "location. Devices here are read from the photographs; the timezone is a\n"
            "guess from activity hours. Both are labelled in the report.\n"
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
    source.add_argument("--volume", default=DEFAULT_VOLUME, help="Docker volume holding the data")
    source.add_argument("--image", default=DEFAULT_IMAGE, help="image used to read the volume")

    output = parser.add_argument_group("output")
    output.add_argument("--days", type=int, default=30, metavar="N", help="window (default 30)")
    output.add_argument("--all", action="store_true", help="every record, ignoring --days")
    output.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    output.add_argument("--csv", type=Path, metavar="PATH", help="write the roster as CSV")
    output.add_argument("--user", type=int, metavar="ID", help="drill into one account")
    output.add_argument(
        "--limit", type=int, default=15, metavar="N", help="rows per table, 0 for all"
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

    now = dt.datetime.now(dt.timezone.utc)
    since = (
        None
        if args.all or args.days <= 0
        else (now - dt.timedelta(days=args.days)).isoformat(timespec="seconds")
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
            database = pull(
                ["ssh", args.ssh, shlex.join(command)], workspace, source, args.volume
            )
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
            stats = collect(conn, since, now)
        finally:
            conn.close()

    if args.csv:
        write_csv(stats, args.csv)
        print(f"wrote {args.csv} ({len(stats['roster'])} accounts)", file=sys.stderr)

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
        return 0
    if args.user:
        return render_user(stats, args.user, ink)
    render(stats, ink, source=source, limit=max(0, args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
