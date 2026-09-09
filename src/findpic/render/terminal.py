"""Rich terminal renderer.

Layout principles, since "readable" was the whole point of the tool:

* The three verdicts lead. They are the answer; everything below is the evidence.
* Every section is a two-column table with a fixed label width, so labels line up
  down the whole report and the eye can scan one column instead of parsing prose.
* Sections with nothing to say are omitted entirely rather than printed empty.
* Colour carries meaning (severity) and is never the *only* carrier — every
  coloured element also has a word or a symbol, so the report survives being
  piped, screenshotted in greyscale, or read by someone colour-blind.
* Every string comes from the message catalogue. Nothing user-facing is written
  in this file.
"""

from __future__ import annotations

import shlex

from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..analysis import fixcmd
from ..i18n import Translator
from ..interpret import (
    Note,
    aspect_ratio,
    describe_accuracy,
    describe_altitude,
    describe_direction,
    describe_light,
    describe_movement,
    describe_orientation_at_capture,
    describe_shutter,
    describe_subject_distance,
    shutter_seconds,
)
from ..models import Category, Finding, Report, Severity, VerdictLevel
from ..recover import PRECISION_SECOND, timestamp_from_filename
from ..tables import (
    COLOR_SPACE_KEYS,
    ENCODING_PROCESS_KEYS,
    EXPOSURE_PROGRAM_KEYS,
    FLASH_KEYS,
    METERING_KEYS,
    ORIENTATION_KEYS,
    SCENE_TYPE_KEYS,
    SPEED_REF_KEYS,
    WHITE_BALANCE_KEYS,
)
from ..util import format_datetime, parse_exif_datetime

# Narrow enough that a 64-character SHA-256 still fits on one line in an
# 80-column terminal, wide enough for the longest label we print.
LABEL_WIDTH = 13

LEVEL_STYLE: dict[VerdictLevel, str] = {
    VerdictLevel.GOOD: "bold green",
    VerdictLevel.FAIR: "bold yellow",
    VerdictLevel.POOR: "bold dark_orange",
    VerdictLevel.BAD: "bold red",
    VerdictLevel.UNKNOWN: "bold grey62",
}

LEVEL_GLYPH: dict[VerdictLevel, str] = {
    VerdictLevel.GOOD: "+",
    VerdictLevel.FAIR: "~",
    VerdictLevel.POOR: "!",
    VerdictLevel.BAD: "x",
    VerdictLevel.UNKNOWN: "?",
}

SEVERITY_STYLE: dict[Severity, str] = {
    Severity.INFO: "cyan",
    Severity.NOTICE: "blue",
    Severity.WARNING: "yellow",
    Severity.CRITICAL: "bold red",
}

SEVERITY_GLYPH: dict[Severity, str] = {
    Severity.INFO: "i",
    Severity.NOTICE: "-",
    Severity.WARNING: "!",
    Severity.CRITICAL: "x",
}

CATEGORY_ORDER = (
    Category.STRUCTURAL,
    Category.AUTHENTICITY,
    Category.PRIVACY,
    Category.AI,
    Category.PLATFORM,
    Category.DEVICE,
)

AXES = ("originality", "privacy", "structure")


def _kv_table() -> Table:
    table = Table(box=None, show_header=False, pad_edge=False, expand=False)
    table.add_column("label", style="grey62", width=LABEL_WIDTH, no_wrap=True)
    table.add_column("value", overflow="fold")
    return table


def _section(console: Console, title: str, table: Table) -> None:
    if not table.row_count:
        return
    console.print(Text(f" {title.upper()}", style="bold grey42"))
    console.print(Padding(table, (0, 0, 1, 1)))


#: Characters a photograph has no business putting on somebody's terminal.
#: ESC is the one that matters — it starts every colour, cursor-move and
#: clear-screen sequence — but a bare CR rewrites the line it is on and a BEL
#: makes the machine chirp, so the whole control range goes.
_CONTROL = dict.fromkeys(range(32), " ") | {0x7F: " "}
#: Bidirectional overrides reverse everything printed after them, which is how
#: "gpj.exe" is made to read as "exe.jpg". findpic's own rules call the pattern
#: out as having no legitimate reason; printing it unchallenged in a report
#: about deception would be an odd thing to do.
_BIDI = {ord(c): None for c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"}


def safe(value: object) -> Text:
    """A metadata value, made safe to put on a terminal.

    Two things, both of which a photograph could do to the reader before this.

    A tag value went into ``Table.add_row`` as a plain string, and rich parses
    a plain string as *markup* — so a ``Software`` tag reading ``[/]`` raised
    ``MarkupError`` and killed the whole run with a traceback. On a tool whose
    job is to be pointed at files from strangers, a file that stops it working
    is a file that wins. Returning ``Text`` is what stops the parse: rich
    renders it literally.

    And nothing filtered control characters, so a ``LensModel`` of
    ``\x1b[41m\x1b[2J`` cleared the screen and repainted it. The bot has
    escaped its output since it was written; the terminal path never did.
    """
    return Text(str(value).translate(_CONTROL).translate(_BIDI))


def _note(note: Note | None, t: Translator) -> str | None:
    """Render an interpretation, resolving any nested catalogue key it carries.

    The ``{name}_key`` convention exists so a rule can hand over "north-north-
    east" as a key rather than a word — the analysis is language-neutral and
    the compass points are not.
    """
    if note is None:
        return None
    params = dict(note.params)
    for name, value in list(params.items()):
        if name.endswith("_key"):
            params[name[: -len("_key")]] = t.get(str(value))
            params.pop(name)
    return t.get(note.key, **params)


def _scaled(note: Note | None) -> Note | None:
    """The bare-fragment form of an interpretation, for use as a table value.

    `interpret` writes complete sentences because the bot prints them as
    sentences. A two-column table has already said "Altitude" in the left
    column, and in Ukrainian the sentence starts with that same word.
    """
    if note is None:
        return None
    return Note(note.key.replace("detail.", "scale.", 1), note.params)


def _exiftool_value(value: object, mapping: dict[str, str], t: Translator) -> object:
    """Translate one of exiftool's decoded English strings, or leave it alone.

    Exact match, never a slug built from the value: an unmapped string would ask
    for a catalogue key that does not exist, and a missing key silently falls
    back to English while failing the catalogue-parity test. Out-of-range tags
    decode as "Unknown (5)", and the raw string is the honest answer for those.
    """
    key = mapping.get(str(value)) if value is not None else None
    return t.get(key) if key else value


def _note_row(
    table: Table, label: str, note: Note | None, t: Translator, raw: object = None
) -> None:
    """A row whose value is an interpretation, with the reading beside it.

    The terminal's advantage over a chat message is that it has room for both.
    "You were travelling ~28 km/h" is what a reader wants; "27.81 km/h" is what
    a forensic reader needs to be able to check it against, so neither is
    dropped.
    """
    rendered = _note(note, t)
    if not rendered:
        return
    if raw not in (None, ""):
        rendered = t.get("ui.value.recorded", value=rendered, raw=raw)
    _add(table, label, rendered)


def _add(table: Table, label: str, value: object, style: str = "") -> None:
    """Add a row, silently skipping anything empty."""
    if value is None or value == "" or value == []:
        return
    cell = safe(value)
    if style:
        cell.stylize(style)
    table.add_row(label, cell)


def render_header(console: Console, report: Report) -> None:
    t = report.translator
    title = Text()
    title.append("findpic", style="bold cyan")
    title.append("  ·  ", style="grey42")
    title.append(report.file.name, style="bold white")
    subtitle = Text(
        t.get(
            "ui.header.subtitle",
            filetype=report.file.file_type or "?",
            size=t.bytes(report.file.size_bytes),
            tags=t.get("ui.header.tags", report.tag_count),
        ),
        style="grey54",
    )
    console.print(Panel(Group(title, subtitle), border_style="grey35", padding=(0, 2)))


def render_verdicts(console: Console, report: Report) -> None:
    t = report.translator
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column("glyph", width=3, no_wrap=True)
    table.add_column("axis", style="grey62", width=15, no_wrap=True)
    table.add_column("label", width=20, no_wrap=True)
    table.add_column("summary", overflow="fold")

    for axis in AXES:
        verdict = report.verdicts.get(axis)
        if verdict is None:
            continue
        style = LEVEL_STYLE[verdict.level]
        table.add_row(
            Text(f" {LEVEL_GLYPH[verdict.level]}", style=style),
            t.get(f"ui.axis.{axis}"),
            Text(verdict.label(t), style=style),
            Text(verdict.summary(t), style="white"),
        )
    console.print(Padding(table, (1, 0, 1, 1)))


def render_device(console: Console, report: Report) -> None:
    t, device = report.translator, report.device
    table = _kv_table()
    _add(
        table,
        t.get("ui.label.camera"),
        device.label if (device.make or device.model) else None,
        "bold white",
    )
    _add(table, t.get("ui.label.system"), device.os)
    if device.host_computer and device.host_computer != device.model:
        _add(table, t.get("ui.label.host"), device.host_computer)

    lens = device.lens_model
    if lens and report.capture.focal_length_35mm:
        # The focal length is stored unit-free so each renderer can attach its
        # own translated "mm"; this is the CLI's half of that bargain.
        lens = t.get(
            "ui.value.lens_equivalent",
            lens=lens,
            focal=t.get("detail.mm", value=report.capture.focal_length_35mm),
        )
    _add(table, t.get("ui.label.lens"), lens)
    _add(
        table,
        t.get("ui.label.mode"),
        " · ".join(t.get(f"mode.{key}") for key in device.capture_mode_keys),
    )
    _add(table, t.get("ui.label.editor"), device.editor, "yellow")
    _add(table, t.get("ui.label.owner"), device.owner, "yellow")
    _add(table, t.get("ui.label.body_serial"), device.body_serial, "yellow")
    _add(table, t.get("ui.label.lens_serial"), device.lens_serial, "yellow")
    if device.uptime_seconds:
        _add(
            table,
            t.get("ui.label.powered_on"),
            t.get("ui.value.powered_on", uptime=t.duration(device.uptime_seconds)),
        )
    if device.has_makernotes:
        _add(
            table,
            t.get("ui.label.makernotes"),
            t.get("ui.value.makernote_present", vendor=device.makernote_vendor),
            "green",
        )
    _section(console, t.get("ui.section.device"), table)


def _taken_row(table: Table, report: Report) -> None:
    """The capture time, from the tags if they still have it and the name if not.

    A messenger deletes the timestamp and then hands the file over under a name
    containing it. findpic already recovers that — and printed it forty-four
    lines lower, under Provenance, while WHEN showed the filesystem mtime. The
    bot has always put it here. This is the terminal catching up.
    """
    t, capture = report.translator, report.capture

    if capture.taken:
        shown = capture.taken
        if capture.taken_subsec:
            # A decimal fraction of a second, not milliseconds: "5" is half a
            # second and "052" is fifty-two thousandths. exiftool preserves the
            # leading zeros (it quotes such values in its JSON), so the digits
            # go in exactly as they came, after the seconds and before the
            # offset. It is extracted, shipped in --json, and was shown nowhere.
            head, _, offset = shown.partition(" +")
            head, _, minus = head.partition(" -") if not offset else (head, "", "")
            tail = f" +{offset}" if offset else (f" -{minus}" if minus else "")
            shown = f"{head}.{capture.taken_subsec}{tail}"
        when = t.describe_when(parse_exif_datetime(capture.taken))
        _add(
            table,
            t.get("ui.label.taken"),
            f"{shown}   ({when})" if when else shown,
            "bold white",
        )
        return

    found = timestamp_from_filename(report.file.name)
    if found is None:
        return
    if found.precision == PRECISION_SECOND:
        value = found.moment.strftime("%Y-%m-%d %H:%M:%S")
        when = t.describe_when(found.moment)
    else:
        # Only the day is known. Printing 00:00:00 would invent an hour.
        value = found.moment.strftime("%Y-%m-%d")
        when = t.get("ui.value.day_only", weekday=t.weekday(found.moment.weekday()))
    # Yellow, not bold white: this is reconstructed, and it must never be
    # mistaken for something the file actually says.
    _add(table, t.get("ui.label.taken"), f"{value}   ({when})" if when else value, "yellow")
    _add(table, "", t.get("ui.value.taken_from_filename"), "grey54")


def render_when(console: Console, report: Report) -> None:
    t, capture = report.translator, report.capture
    table = _kv_table()

    _taken_row(table, report)
    if capture.taken_offset:
        _add(
            table,
            t.get("ui.label.timezone"),
            t.get("ui.value.from_offset_tag", offset=capture.taken_offset),
        )
    if capture.digitised_differs:
        _add(table, t.get("ui.label.digitised"), capture.digitized)
    if capture.modified:
        if capture.modified_matches_taken:
            _add(table, t.get("ui.label.modified"), t.get("ui.value.unchanged"), "green")
        else:
            _add(table, t.get("ui.label.modified"), capture.modified, "yellow")
    _add(table, t.get("ui.label.gps_clock"), capture.gps_utc)
    # The filesystem timestamp is about the copy on this disk, not the photo, so
    # it comes last — and only when something above it is about the photograph.
    # On a stripped file it was the entire WHEN section: a heading promising
    # when the picture was taken, under which sat the date this copy happened to
    # be written to this disk.
    if table.row_count:
        _add(
            table,
            t.get("ui.label.file_saved"),
            format_datetime(parse_exif_datetime(report.file.modified)),
        )
    _section(console, t.get("ui.section.when"), table)


def render_where(console: Console, report: Report) -> None:
    """Where it was taken, with every number given a scale.

    Each row here used to be a bare measurement: "±21.8535 m", "349° N",
    "27.81 km/h". `interpret.py` has turned each of those into a sentence since
    it was written, in both languages, and the terminal imported none of it —
    so the front end with the most room to explain explained the least.
    """
    t, location = report.translator, report.location
    if not location.present:
        return
    table = _kv_table()

    _add(table, t.get("ui.label.coordinates"), location.decimal, "bold white")
    if location.accuracy_m is not None:
        # Not `detail.accuracy.*` as a row value: in Ukrainian those embed the
        # word "Точність", which is the label this row already carries.
        scale = describe_accuracy(location.accuracy_m)
        if scale is not None:
            _add(
                table,
                t.get("ui.label.accuracy"),
                t.get(
                    "ui.value.accuracy_row",
                    metres=scale.params.get("value", location.accuracy_m),
                    scale=t.get(scale.key.replace("detail.accuracy.", "scale.accuracy.")),
                ),
            )
    _add(table, t.get("ui.label.dms"), location.dms)
    if location.place:
        _add(table, t.get("ui.label.place"), location.place, "bold white")
    elif location.geocode_error:
        _add(
            table,
            t.get("ui.label.place"),
            t.get("ui.value.place_unresolved", reason=location.geocode_error),
            "grey54",
        )

    if location.altitude_m is not None:
        # Shared with the bot, and it takes abs() there: exiftool already signs
        # the value for GPSAltitudeRef=1, so a Dead Sea photo printed
        # "-413.2 m below sea level" — literally above.
        _note_row(
            table,
            t.get("ui.label.altitude"),
            # `scale.` rather than `detail.`, for the same reason as accuracy
            # above: the `detail.*` forms are whole sentences written for the
            # bot, and in Ukrainian they open with the word this row's label
            # already carries — "Висота  Висота 325 м над рівнем моря".
            _scaled(
                describe_altitude(
                    location.altitude_m,
                    below=(location.altitude_ref or "").lower().startswith("below"),
                )
            ),
            t,
        )

    if location.direction_deg is not None:
        reference = (location.direction_ref or "").strip().lower()
        # Three states, not two. Exif has no default for GPSImgDirectionRef, so
        # an absent one is "the file did not say" rather than "true north".
        magnetic = None if not reference else reference.startswith("m")
        _note_row(
            table, t.get("ui.label.facing"), describe_direction(location.direction_deg, magnetic), t
        )

    if location.speed is not None:
        _note_row(
            table,
            t.get("ui.label.movement"),
            describe_movement(location.speed, location.speed_ref),
            t,
            raw=(
                f"{location.speed:g} {_exiftool_value(location.speed_ref, SPEED_REF_KEYS, t)}"
                if location.speed_ref
                else None
            ),
        )

    if location.present:
        # The URL itself, not the words "open in OpenStreetMap".
        #
        # It was rendered as an OSC 8 hyperlink over a label, and rich emits
        # that correctly — but a terminal that does not support OSC 8 shows the
        # label and nothing else, so the reader has a map they cannot open and
        # a URL they cannot copy. Printing the address works everywhere: it is
        # selectable, most terminals linkify a bare URL of their own accord, and
        # where OSC 8 *is* supported it is still one click.
        #
        # osm.org rather than openstreetmap.org, and no #map fragment: the
        # canonical form is 87 characters and folds across two lines in an
        # 80-column terminal, which is exactly what makes a URL uncopyable.
        # This is 46 and points at the same marker. `location.osm_url` keeps
        # the long form for the JSON output and the bot, where width is not a
        # constraint.
        url = f"https://osm.org/?mlat={location.latitude:.6f}&mlon={location.longitude:.6f}"
        table.add_row(t.get("ui.label.map"), Text(url, style=f"blue underline link {url}"))
    _section(console, t.get("ui.section.where"), table)


def _encoding(report: Report, t: Translator) -> object:
    """How the pixels are stored, with the detail a forensic reader wants.

    Chroma subsampling is not decoration in a tool whose originality axis is
    built on how a file was compressed: 4:2:0 against 4:4:4 separates a camera
    pipeline from a re-encoder.

    All three of the extra fields come from exiftool's ``File:`` group, which
    does not exist on a HEIC — where ``encoding_process`` is absent too, so the
    row silently disappeared. The QuickTime profile is the equivalent fact.
    """
    image = report.image
    process = _exiftool_value(image.encoding_process, ENCODING_PROCESS_KEYS, t)
    if process is None:
        return report.raw.get("QuickTime:GeneralProfileIDC")
    if not (image.subsampling and image.bits_per_sample and image.color_components):
        return process
    return t.get(
        "ui.value.encoding_detail",
        process=process,
        subsampling=image.subsampling,
        bits=image.bits_per_sample,
        components=image.color_components,
    )


def render_image(console: Console, report: Report) -> None:
    t, image, capture = report.translator, report.image, report.capture
    table = _kv_table()

    dimensions = image.dimensions
    ratio = aspect_ratio(image.width, image.height)
    if dimensions and image.megapixels:
        dimensions = t.get(
            "ui.value.dimensions_ratio" if ratio else "ui.value.dimensions",
            size=dimensions,
            megapixels=f"{image.megapixels:.1f}",
            ratio=ratio or "",
        )
    _add(table, t.get("ui.label.dimensions"), dimensions)
    _add(
        table,
        t.get("ui.label.orientation"),
        _exiftool_value(image.orientation, ORIENTATION_KEYS, t),
    )
    # Directly under Orientation on purpose: that row is the display flag the
    # file carries, this one is how the device was actually being held, and a
    # reader can only notice they disagree when the two sit together.
    _note_row(
        table,
        t.get("ui.label.held"),
        describe_orientation_at_capture(report.raw.get("Apple:AccelerationVector")),
        t,
    )

    exposure = " · ".join(
        part
        for part in (
            f"ISO {capture.iso}" if capture.iso else None,
            f"f/{capture.f_number:g}" if capture.f_number else None,
            t.get("detail.seconds", value=capture.exposure_time) if capture.exposure_time else None,
            t.get("detail.mm", value=capture.focal_length) if capture.focal_length else None,
        )
        if part
    )
    _add(table, t.get("ui.label.exposure"), exposure)
    # Five readings that already existed, were already translated, and reached
    # only the bot. The terminal has more room than a chat message and was
    # showing less.
    _note_row(table, t.get("ui.label.light"), describe_light(capture.light_value), t)
    _note_row(
        table,
        t.get("ui.label.shutter"),
        _scaled(
            describe_shutter(
                shutter_seconds(
                    report.raw.get("ExifIFD:ExposureTime")
                    or report.raw.get("Composite:ShutterSpeed")
                ),
                # The tag's presence only. exiftool does not decode OISMode, so
                # what the value means is not established.
                stabilised=report.raw.get("Apple:OISMode") is not None,
            )
        ),
        t,
    )
    _note_row(
        table,
        t.get("ui.label.focus"),
        describe_subject_distance(report.raw.get("Apple:FocusDistanceRange")),
        t,
        raw=report.raw.get("Apple:FocusDistanceRange"),
    )
    _add(table, t.get("ui.label.flash"), _exiftool_value(capture.flash, FLASH_KEYS, t))
    _add(
        table,
        t.get("ui.label.program"),
        _exiftool_value(capture.exposure_program, EXPOSURE_PROGRAM_KEYS, t),
    )
    _add(
        table,
        t.get("ui.label.colour"),
        image.icc_profile or _exiftool_value(image.color_space, COLOR_SPACE_KEYS, t),
    )
    _add(
        table,
        t.get("ui.label.metering"),
        " · ".join(
            str(_exiftool_value(value, mapping, t))
            for value, mapping in (
                (capture.metering_mode, METERING_KEYS),
                (capture.white_balance, WHITE_BALANCE_KEYS),
                (capture.scene_capture_type, SCENE_TYPE_KEYS),
            )
            if value
        ),
    )
    _add(table, t.get("ui.label.encoding"), _encoding(report, t))
    if image.has_thumbnail and image.thumbnail_size:
        _add(
            table,
            t.get("ui.label.thumbnail"),
            t.get("ui.value.thumbnail", bytes=t.bytes(image.thumbnail_size)),
        )
    _section(console, t.get("ui.section.image"), table)


#: Not the file's own metadata: the filesystem's view of it, and exiftool's own
#: derived values. "Other" is not a group at all — group_counts() files any key
#: without a colon there, and the only such key is exiftool's echo of the path.
UNCOUNTED_GROUPS = frozenset({"System", "File", "ExifTool", "Composite", "Other"})

#: How many groups are named before the tail is folded into "+N more".
GROUPS_SHOWN = 5


def render_people(console: Console, report: Report) -> None:
    if not report.people:
        return
    t = report.translator
    table = _kv_table()
    _add(
        table,
        t.get("ui.label.regions"),
        t.get("ui.value.regions", len(report.people)),
        "yellow",
    )
    # Where in the picture. Extracted, carried in --json twice, and shown by
    # neither renderer — a count alone says people are present, this says where
    # the camera thought they were.
    for person in report.people[:4]:
        if None in (person.x, person.y, person.w, person.h):
            # Three of the four ways a region is built carry no geometry at all
            # (a bare name from Microsoft's People tags, or from PersonInImage).
            continue
        _add(
            table,
            t.get("ui.label.frame"),
            t.get(
                "ui.value.face_at",
                # MWG stores the region's *centre*, not its top-left corner.
                x=f"{person.x * 100:.0f}",
                y=f"{person.y * 100:.0f}",
                w=f"{person.w * 100:.0f}",
                h=f"{person.h * 100:.0f}",
            ),
        )
    # The coordinates are in the frame the sensor wrote, and this file may carry
    # a rotation flag — so on a phone photo held upright, "48% down" is not 48%
    # down the picture the reader is looking at. Said once, under the rows.
    if report.image.orientation and any(p.x is not None for p in report.people):
        _add(table, "", t.get("ui.value.face_sensor_frame"), "grey54")
    named = [p.name for p in report.people if p.name]
    if named:
        _add(table, t.get("ui.label.named_people"), ", ".join(named), "bold yellow")
    _section(console, t.get("ui.section.people"), table)


def _tag_groups(report: Report, show_all: bool = False) -> str:
    """Which namespaces the tags live in, biggest first.

    Computed on every run, stored on the model, emitted in --json, and rendered
    by nobody. It is the cheapest orientation a forensic reader gets: "Apple 34"
    says the vendor block survived, and its absence says it did not.

    ``report.groups`` already arrives sorted by count then name, so this only
    filters and folds.
    """
    t = report.translator
    counted = {name: count for name, count in report.groups.items() if name not in UNCOUNTED_GROUPS}
    if not counted:
        # A fully stripped file can leave nothing but System/File/Composite.
        return ""
    names = list(counted)
    shown = names if show_all else names[:GROUPS_SHOWN]
    line = " · ".join(f"{name} {counted[name]}" for name in shown)
    if len(names) > len(shown):
        line += " · " + t.get("ui.value.more_groups", count=len(names) - len(shown))
    return line


def render_integrity(console: Console, report: Report, show_all_groups: bool = False) -> None:
    t = report.translator
    table = _kv_table()
    _add(table, t.get("ui.label.sha256"), report.file.sha256, "grey62")
    _add(table, t.get("ui.label.md5"), report.file.md5, "grey62")
    _add(table, t.get("ui.label.mime"), report.file.mime_type)
    _add(table, t.get("ui.label.tag_groups"), _tag_groups(report, show_all_groups), "grey62")
    _section(console, t.get("ui.section.file"), table)


#: What the command under a finding actually does. Three of the thirteen do not
#: remove anything — one writes a date back in, one extracts a preview — and all
#: three were printed under a label that reads, in Ukrainian, "how to remove".
FIX_LABEL = {
    "remove": "ui.value.fix",
    "inspect": "ui.value.fix_inspect",
    "restore": "ui.value.fix_restore",
}


def _combined_args(entries: list[Finding]) -> tuple[tuple[str, ...], list[str]]:
    """The union of every removal flag in one category, and what it costs.

    Printed as a single command because the individual ones cannot be run in
    sequence: each reads the original and each writes the same output name, so
    the first succeeded, the next five refused to start, and the reader was left
    holding a file findpic said it had cleaned that still carried five leaks.
    """
    args: list[str] = []
    costs: list[str] = []
    for finding in entries:
        if finding.remediation_kind != "remove":
            continue
        args.extend(finding.remediation_args)
        if finding.remediation_cost_key:
            costs.append(finding.remediation_cost_key)
    return tuple(dict.fromkeys(args)), list(dict.fromkeys(costs))


def _fix_rows(body: Text, finding: Finding, t: Translator) -> None:
    """The command, and — where it takes more than it was asked to — the cost."""
    if not finding.remediation:
        return
    body.append("\n")
    body.append(t.get(FIX_LABEL.get(finding.remediation_kind, "ui.value.fix")), style="green bold")
    # safe(): the command carries the file's own name, and a filename can hold
    # ANSI escapes that repaint the terminal.
    printed = safe(finding.remediation)
    printed.stylize("green")
    body.append_text(printed)
    if finding.remediation_cost_key:
        body.append("\n")
        body.append(t.get("ui.value.fix_cost"), style="yellow")
        body.append(t.get(f"ui.value.fix_cost.{finding.remediation_cost_key}"), style="grey54")


def _finding_table(entries: list[Finding], t: Translator, report: Report | None = None) -> Table:
    """Render findings in a glyph/body grid so wrapped text keeps its indent."""
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 0, 1, 0))
    table.add_column("glyph", width=3, no_wrap=True, vertical="top")
    table.add_column("body", overflow="fold", ratio=1)

    for finding in entries:
        body = Text()
        body.append(finding.title(t), style="bold white")
        if finding.confidence.value != "high":
            body.append(
                t.get(
                    "ui.value.confidence",
                    confidence=t.get(f"ui.confidence.{finding.confidence.value}"),
                ),
                style="grey42",
            )
        detail = finding.detail(t)
        if detail:
            body.append("\n")
            body.append(detail, style="grey62")
        _fix_rows(body, finding, t)
        table.add_row(
            Text(
                f" {SEVERITY_GLYPH[finding.severity]}",
                style=SEVERITY_STYLE[finding.severity],
            ),
            body,
        )

    # One line that does the whole category, and the pointer to --backup beneath
    # it. This is the line most readers will paste, and it is the last moment
    # before the metadata is gone — which is exactly where the epilog's advice to
    # make a copy first was not being said.
    args, costs = _combined_args(entries)
    if report is not None and len(args) > 1:
        combined = Text()
        combined.append(t.get("ui.value.fix_all"), style="green bold")
        printed = safe(fixcmd.command(report.file.path, report.file.file_type_extension, args))
        printed.stylize("green")
        combined.append_text(printed)
        for cost in costs:
            combined.append("\n")
            combined.append(t.get("ui.value.fix_cost"), style="yellow")
            combined.append(t.get(f"ui.value.fix_cost.{cost}"), style="grey54")
        combined.append("\n")
        hint = safe(t.get("ui.hint.backup_first", file=shlex.quote(report.file.path)))
        hint.stylize("grey42")
        combined.append_text(hint)
        table.add_row(Text(""), combined)
    return table


#: The two findings that explain an empty report: that the file came through a
#: messenger, and that it is a screen capture rather than a photograph. They are
#: INFO by findpic's own classification, so --quiet removed them — and on a
#: screenshot that leaves a report saying "LIKELY ORIGINAL" with nothing
#: anywhere to say nothing was ever photographed. Screenshot first: a screen
#: capture that also lost its tags is still, first, a screen capture.
PROVENANCE_FINDINGS = ("recovery.screenshot", "platform.stripped")


def render_provenance(console: Console, report: Report) -> tuple[str, ...]:
    """The one line that has to come before a verdict shaped like failure.

    Above the verdicts rather than below them. The bot, which has done this for
    longer, suppresses its verdict badge entirely here on the reasoning that a
    verdict printed under a note explaining why the file has nothing to judge is
    the same failure said twice; the terminal keeps its three axes but puts the
    explanation first, so the reader has it before the grades rather than after.

    Returns the ids it printed, so FINDINGS can leave them out instead of
    saying the same thing twice in one report.
    """
    found = {finding.id: finding for finding in report.findings}
    t = report.translator
    for finding in (found.get(fid) for fid in PROVENANCE_FINDINGS):
        if finding is None:
            continue
        body = Text()
        body.append(finding.title(t), style="bold white")
        detail = finding.detail(t)
        if detail:
            body.append("\n")
            body.append(detail, style="grey62")
        console.print(Padding(body, (1, 0, 0, 1)))
        return (finding.id,)
    return ()


def render_findings(
    console: Console,
    report: Report,
    show_info: bool = True,
    skip: tuple[str, ...] = (),
) -> None:
    t = report.translator
    # A verdict must never stand above nothing. --quiet drops INFO findings, and
    # on a file whose findings are *all* INFO that left "Privacy LOW EXPOSURE —
    # a few details leak" over an empty list: the report asserting a leak and
    # then declining to name it. Severity and weight are separate fields, so an
    # INFO finding can still be what a verdict is built on; those stay.
    cited = {
        finding.id
        for verdict in report.verdicts.values()
        if verdict.level.rank > VerdictLevel.GOOD.rank
        for finding in verdict.reasons
    }
    findings = [
        f
        for f in report.sorted_findings
        if f.id not in skip and (show_info or f.severity.rank > Severity.INFO.rank or f.id in cited)
    ]
    if not findings:
        return

    console.print(Text(f" {t.get('ui.section.findings').upper()}", style="bold grey42"))
    by_category: dict[Category, list[Finding]] = {}
    for finding in findings:
        by_category.setdefault(finding.category, []).append(finding)

    for category in CATEGORY_ORDER:
        entries = by_category.get(category)
        if not entries:
            continue
        console.print(
            Padding(Text(t.get(f"ui.category.{category.value}"), style="grey54"), (0, 0, 0, 1))
        )
        # Rich drops the last row's bottom padding, so separate the category
        # blocks explicitly rather than letting them run together.
        console.print(
            Padding(
                _finding_table(entries, t, report if category is Category.PRIVACY else None),
                (0, 0, 1, 1),
            )
        )


def render_notes(console: Console, report: Report) -> None:
    if not (report.exiftool_warnings or report.errors):
        return
    from ..util import truncate

    t = report.translator
    table = _kv_table()
    for warning in report.exiftool_warnings:
        _add(table, t.get("ui.label.warning"), truncate(warning, 160), "grey54")
    for error in report.errors:
        _add(table, t.get("ui.label.error"), truncate(error, 160), "red")
    _section(console, t.get("ui.section.notes"), table)


def unreadable(report: Report) -> bool:
    """Whether exiftool could not make sense of this file at all.

    Both conditions, deliberately. A format error alone turns up on files that
    are otherwise perfectly readable — a truncated thumbnail, a malformed XMP
    packet — and those still deserve a report. It is the *combination* with no
    identified file type that means there was nothing to read.
    """
    return bool(report.errors) and not report.file.file_type


def render_report(
    console: Console,
    report: Report,
    show_info: bool = True,
    show_notes: bool = False,
) -> None:
    """Print one complete report."""
    render_header(console, report)
    if unreadable(report):
        # Three confident verdicts on 800 bytes of random data — including
        # "Privacy CLEAN, this file carries almost no metadata, so there is
        # nothing to leak" — is the tool grading a file it could not open.
        # exiftool said "File format error"; findpic collected it and then hid
        # it behind --notes. Say it instead, and grade nothing.
        render_notes(console, report)
        return
    hoisted = render_provenance(console, report)
    render_verdicts(console, report)
    render_device(console, report)
    render_when(console, report)
    render_where(console, report)
    render_image(console, report)
    render_people(console, report)
    render_findings(console, report, show_info=show_info, skip=hoisted)
    render_integrity(console, report, show_all_groups=show_notes)
    if show_notes:
        render_notes(console, report)
