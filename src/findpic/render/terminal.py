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

from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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


def render_when(console: Console, report: Report) -> None:
    t, capture = report.translator, report.capture
    table = _kv_table()

    if capture.taken:
        when = t.describe_when(parse_exif_datetime(capture.taken))
        _add(
            table,
            t.get("ui.label.taken"),
            f"{capture.taken}   ({when})" if when else capture.taken,
            "bold white",
        )
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
    # it comes last and normalised out of exiftool's colon-separated format.
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
            describe_altitude(
                location.altitude_m,
                below=(location.altitude_ref or "").lower().startswith("below"),
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
            raw=f"{location.speed:g} {location.speed_ref}" if location.speed_ref else None,
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
    _add(table, t.get("ui.label.orientation"), image.orientation)
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
        describe_shutter(
            shutter_seconds(
                report.raw.get("ExifIFD:ExposureTime") or report.raw.get("Composite:ShutterSpeed")
            ),
            # The tag's presence only. exiftool does not decode OISMode, so
            # what the value means is not established.
            stabilised=report.raw.get("Apple:OISMode") is not None,
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
    _add(table, t.get("ui.label.flash"), capture.flash)
    _add(table, t.get("ui.label.program"), capture.exposure_program)
    _add(table, t.get("ui.label.colour"), image.icc_profile or image.color_space)
    _add(table, t.get("ui.label.encoding"), image.encoding_process)
    if image.has_thumbnail and image.thumbnail_size:
        _add(
            table,
            t.get("ui.label.thumbnail"),
            t.get("ui.value.thumbnail", bytes=t.bytes(image.thumbnail_size)),
        )
    _section(console, t.get("ui.section.image"), table)


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
    named = [p.name for p in report.people if p.name]
    if named:
        _add(table, t.get("ui.label.named_people"), ", ".join(named), "bold yellow")
    _section(console, t.get("ui.section.people"), table)


def render_integrity(console: Console, report: Report) -> None:
    t = report.translator
    table = _kv_table()
    _add(table, t.get("ui.label.sha256"), report.file.sha256, "grey62")
    _add(table, t.get("ui.label.md5"), report.file.md5, "grey62")
    _add(table, t.get("ui.label.mime"), report.file.mime_type)
    _section(console, t.get("ui.section.file"), table)


def _finding_table(entries: list[Finding], t: Translator) -> Table:
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
        if finding.remediation:
            body.append("\n")
            body.append(t.get("ui.value.fix"), style="green bold")
            body.append(finding.remediation, style="green")
        table.add_row(
            Text(
                f" {SEVERITY_GLYPH[finding.severity]}",
                style=SEVERITY_STYLE[finding.severity],
            ),
            body,
        )
    return table


def render_findings(console: Console, report: Report, show_info: bool = True) -> None:
    t = report.translator
    findings = [
        f for f in report.sorted_findings if show_info or f.severity.rank > Severity.INFO.rank
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
        console.print(Padding(_finding_table(entries, t), (0, 0, 1, 1)))


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
    render_verdicts(console, report)
    render_device(console, report)
    render_when(console, report)
    render_where(console, report)
    render_image(console, report)
    render_people(console, report)
    render_findings(console, report, show_info=show_info)
    render_integrity(console, report)
    if show_notes:
        render_notes(console, report)
