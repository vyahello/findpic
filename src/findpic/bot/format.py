"""Render a :class:`Report` as a Telegram message.

The shape is driven by one rule: **say each thing once, and say it in a form the
reader can use.**

That rules out two habits the terminal report can afford and a chat message
cannot. First, a verdict banner: three coloured labels at the top summarise
information the message is about to show anyway, and on a phone they push the
actual content below the fold. Only originality survives, because "is this the
picture that came out of the camera" is a genuine yes/no that nothing else
answers.

Second, restating. The location appears under ДЕ, so the privacy section does
not explain the location again — it names what would leave with the file and
stops. Altitude and bearing sit beside the place they describe, phrased as "the
camera was pointing north-east" rather than "GPSImgDirection 29.18", because a
number nobody can interpret is not information.

Everything interpolated is escaped — metadata is attacker-controlled, and a
caption containing markup would otherwise become markup.
"""

from __future__ import annotations

import datetime as dt
from html import escape

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
)
from ..models import Category, Report, Severity, VerdictLevel
from ..recover import timestamp_from_filename
from ..util import parse_exif_datetime

#: Telegram's hard limit. We aim below it and fold the rest away.
MESSAGE_LIMIT = 4096
SAFE_LIMIT = 3500

ORIGINALITY_MARK: dict[VerdictLevel, str] = {
    VerdictLevel.GOOD: "✅",
    VerdictLevel.FAIR: "☑️",
    VerdictLevel.POOR: "✂️",
    VerdictLevel.BAD: "⚠️",
    VerdictLevel.UNKNOWN: "❔",
}

#: What the compression admits when the tags are gone. Deliberately short: the
#: restart-interval and one-dpi findings corroborate these and would read as
#: filler beside them, so they stay in the terminal report and the tag dump.
TRACE_FINDINGS = (
    "recovery.encoder_library",
    "recovery.encoder_vendor",
    "recovery.container_rewritten",
    "recovery.preview_older",
    "recovery.preview_shape",
    "recovery.screenshot",
)

#: Privacy findings whose substance is already on screen in another section.
#: Repeating them is what made the old privacy block read as filler.
ALREADY_SHOWN = {
    "privacy.gps_detail",  # altitude and bearing live under ДЕ
    "privacy.face_regions",  # the reader did not ask about faces
    "privacy.embedded_thumbnail",  # normal for every camera file
    "privacy.timezone",  # shown under КОЛИ
    "privacy.device_uptime",  # a curiosity, not something to act on
}


def esc(value: object) -> str:
    """HTML-escape anything before it reaches a Telegram message."""
    return escape(str(value), quote=False)


def _note(note: Note | None, translator: Translator) -> str | None:
    """Render an interpretation, resolving any nested catalogue key it carries."""
    if note is None:
        return None
    params = dict(note.params)
    for name, value in list(params.items()):
        if name.endswith("_key"):
            params[name[: -len("_key")]] = translator.get(str(value))
            params.pop(name)
    return translator.get(note.key, **params)


def _block(title: str, lines: list[str]) -> list[str]:
    lines = [line for line in lines if line]
    if not lines:
        return []
    return ["", f"<b>{esc(title)}</b>", *lines]


# ------------------------------------------------------------------ sections


def render_headline(report: Report) -> list[str]:
    """Filename, size, and the one verdict worth stating up front."""
    t = report.translator
    size_line = f"{t.bytes(report.file.size_bytes)} · {report.file.file_type or '?'}"
    if report.image.width and report.image.height:
        size_line += f" · {report.image.width}×{report.image.height}"

    lines = [f"🔎 <b>{esc(report.file.name)}</b>", f"<i>{esc(size_line)}</i>"]

    verdict = report.verdicts.get("originality")
    if verdict is not None:
        lines += [
            "",
            f"{ORIGINALITY_MARK[verdict.level]} <b>{esc(verdict.label(t))}</b>",
            f"<i>{esc(t.get(f'bot.originality.{verdict.level.value}'))}</i>",
        ]
    return lines


def render_device(report: Report) -> list[str]:
    t, device, capture = report.translator, report.device, report.capture
    if not (device.make or device.model):
        return []

    head = esc(device.label)
    if device.os:
        head = f"{head} · {esc(device.os)}"

    second: list[str] = []
    # "Mode: Photo" on a photo bot says nothing; portrait or ProRAW does.
    modes = [key for key in device.capture_mode_keys if key != "photo"]
    if modes:
        second.append(" · ".join(t.get(f"mode.{key}") for key in modes))
    if capture.focal_35mm:
        second.append(
            t.get("detail.equivalent", value=t.get("detail.mm", value=f"{capture.focal_35mm:g}"))
        )

    lines = [head]
    if second:
        lines.append(esc(" · ".join(second)))
    if device.editor:
        lines.append(f"✏️ {esc(device.editor)}")
    return _block(t.get("bot.section.device"), lines)


def _format_date(moment: dt.datetime, translator: Translator) -> str:
    """A date a person reads, to the second.

    Seconds matter here: two photos a few seconds apart is exactly the kind of
    detail someone checks a timestamp for.
    """
    return translator.get(
        "time.full",
        day=moment.day,
        month=translator.get(f"time.month.{moment.month}"),
        year=moment.year,
        time=moment.strftime("%H:%M:%S"),
    )


def _format_day(moment: dt.datetime, translator: Translator) -> str:
    """A date with no time, for when only the day is known."""
    return translator.get(
        "time.day",
        day=moment.day,
        month=translator.get(f"time.month.{moment.month}"),
        year=moment.year,
    )


def render_when(report: Report) -> list[str]:
    t, capture = report.translator, report.capture
    if not capture.taken:
        return _render_when_from_filename(report)

    moment = parse_exif_datetime(capture.taken)
    lines: list[str] = []
    if moment is not None:
        lines.append(f"<b>{esc(_format_date(moment, t))}</b>")
        # The UTC offset is deliberately not shown. It is local time — which is
        # what the reader expects — and "UTC+02:00" means nothing to most people
        # while looking like it should. It stays in the full tag dump.
        when = t.describe_when(moment)
        if when:
            lines.append(esc(when))
    else:
        lines.append(f"<b>{esc(capture.taken)}</b>")

    if capture.modified and capture.modified_matches_taken is False:
        lines.append(f"✏️ {esc(t.get('bot.when.modified', value=capture.modified))}")
    return _block(t.get("bot.section.when"), lines)


def _render_when_from_filename(report: Report) -> list[str]:
    """The capture time when the tags no longer carry one.

    A messenger deletes the timestamp and then hands the file over under a name
    containing it. That is the one piece of what was removed that can genuinely
    be put back, so it belongs under КОЛИ like any other capture time — but
    labelled, because it came from the name and a name can be changed.
    """
    t = report.translator
    found = timestamp_from_filename(report.file.name)
    if found is None:
        return []

    if found.is_exact:
        lines = [f"<b>{esc(_format_date(found.moment, t))}</b>"]
        when = t.describe_when(found.moment)
        if when:
            lines.append(esc(when))
    else:
        # Only the day is known. Printing 00:00:00 would invent an hour.
        lines = [f"<b>{esc(_format_day(found.moment, t))}</b>"]
    lines.append(f"📄 {esc(t.get(f'bot.when.from_filename.{found.precision}'))}")
    return _block(t.get("bot.section.when"), lines)


def render_traces(report: Report) -> list[str]:
    """What the compression says, on a file whose tags are gone.

    Only for that case. On a photo that still knows its own camera these would
    be structural trivia buried under the answers the reader actually wanted;
    on a stripped one they are the only answers there are, and without them the
    message reads as "findpic found nothing" rather than "there is nothing left
    to find".

    Titles only. The reasoning behind each is long, correctly so in a terminal,
    and would swamp a chat message.
    """
    t = report.translator
    if report.capture.taken or report.device.make or report.device.model:
        return []
    lines = [
        f"• {esc(finding.title(t))}"
        for finding in report.sorted_findings
        if finding.id in TRACE_FINDINGS
    ]
    return _block(t.get("bot.section.traces"), lines)


def render_where(report: Report) -> list[str]:
    """Location, with every number turned into a statement.

    Altitude, bearing and speed belong here — beside the place they describe —
    rather than inside a privacy warning that repeats the coordinates.
    """
    t, location = report.translator, report.location
    if not location.present:
        return []

    lines: list[str] = []
    if location.place:
        lines.append(f"<b>{esc(location.place)}</b>")
    lines.append(f"<code>{esc(location.decimal)}</code>")

    for note in (
        describe_accuracy(location.accuracy_m),
        describe_altitude(
            location.altitude_m,
            below=(location.altitude_ref or "").lower().startswith("below"),
        ),
        describe_direction(
            location.direction_deg,
            magnetic=(location.direction_ref or "").lower().startswith("m"),
        ),
        describe_movement(location.speed, location.speed_ref),
    ):
        rendered = _note(note, t)
        if rendered:
            lines.append(esc(rendered))

    if location.osm_url:
        lines.append(f'<a href="{esc(location.osm_url)}">🗺 {esc(t.get("bot.map.open"))}</a>')
    return _block(t.get("bot.section.where"), lines)


def _shutter_seconds(report: Report) -> float | None:
    raw = report.raw.get("ExifIFD:ExposureTime") or report.raw.get("Composite:ShutterSpeed")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def render_shot(report: Report) -> list[str]:
    """The photographic facts, plus what they imply about the moment."""
    t, image, capture = report.translator, report.image, report.capture
    lines: list[str] = []

    geometry = []
    if image.megapixels:
        geometry.append(t.get("detail.megapixels", value=f"{image.megapixels:.1f}"))
    if image.dimensions:
        geometry.append(f"{image.width} × {image.height}")
    if ratio := aspect_ratio(image.width, image.height):
        geometry.append(ratio)
    if geometry:
        lines.append(esc(" · ".join(geometry)))

    exposure = [
        piece
        for piece in (
            f"ISO {capture.iso}" if capture.iso else None,
            f"f/{capture.f_number:g}" if capture.f_number else None,
            t.get("detail.seconds", value=capture.exposure_time) if capture.exposure_time else None,
            t.get("detail.mm", value=f"{capture.focal_mm:g}") if capture.focal_mm else None,
        )
        if piece
    ]
    if exposure:
        lines.append(f"<code>{esc(' · '.join(exposure))}</code>")

    stabilised = report.raw.get("Apple:OISMode") is not None
    for note in (
        describe_light(capture.light_value),
        describe_shutter(_shutter_seconds(report), stabilised=bool(stabilised)),
        describe_subject_distance(report.raw.get("Apple:FocusDistanceRange")),
        describe_orientation_at_capture(report.raw.get("Apple:AccelerationVector")),
    ):
        rendered = _note(note, t)
        if rendered:
            lines.append(esc(rendered))

    flash = (capture.flash or "").lower()
    if "did not fire" in flash:
        lines.append(esc(t.get("detail.flash.off")))
    elif "fired" in flash:
        lines.append(esc(t.get("detail.flash.on")))

    if image.icc_profile:
        lines.append(esc(t.get("detail.colour", value=image.icc_profile)))
    return _block(t.get("bot.section.shot"), lines)


def render_exposure_risks(report: Report) -> list[str]:
    """What would leave with the file — named, not re-explained.

    Each line is a thing to remove, not a paragraph about why metadata exists.
    Anything already visible in another section is filtered out.
    """
    t = report.translator
    items: list[str] = []
    for finding in report.sorted_findings:
        if finding.category is not Category.PRIVACY:
            continue
        if finding.id in ALREADY_SHOWN or finding.severity is Severity.INFO:
            continue
        key = f"bot.leak.{finding.id.split('.', 1)[1]}"
        label = t.get(key, **finding.resolve_params(t)) if t.has(key) else finding.title(t)
        items.append(f"• {esc(label)}")

    return _block(t.get("bot.section.leaks"), items)


def render_warnings(report: Report) -> list[str]:
    """Structural problems. Rare, and the only thing allowed to shout."""
    t = report.translator
    lines = [
        f"🔴 {esc(finding.title(t))}"
        for finding in report.sorted_findings
        if finding.category is Category.STRUCTURAL
        and finding.severity.rank >= Severity.WARNING.rank
    ]
    return _block(t.get("bot.section.warnings"), lines)


# -------------------------------------------------------------------- public


def render_report(report: Report, *, source_note: str = "") -> str:
    """Build the complete message body."""
    parts = render_headline(report)
    if source_note:
        parts += ["", source_note]

    parts += (
        render_warnings(report)
        + render_device(report)
        + render_when(report)
        + render_where(report)
        + render_shot(report)
        + render_traces(report)
        + render_exposure_risks(report)
    )

    return _fit(parts, report.translator)


def _fit(parts: list[str], t: Translator) -> str:
    """Join the lines, dropping whole lines rather than cutting one in half.

    Slicing a string of HTML at an arbitrary offset lands inside a tag about as
    often as not, and Telegram answers an unbalanced entity with
    ``Bad Request: can't parse entities`` — which means the user receives
    *nothing at all*, not a shortened report. Truncating on line boundaries
    cannot produce that, because every line this module emits is balanced on its
    own.

    ``SAFE_LIMIT`` rather than ``MESSAGE_LIMIT`` leaves room for the note that
    says something was left out; the note is the difference between a report
    that looks finished and one the reader knows to follow up.
    """
    message = "\n".join(parts)
    if len(message) <= SAFE_LIMIT:
        return message

    # Skip what does not fit rather than stopping at it. One pathological tag —
    # a 4 kB place name, a colour profile with a novel in it — would otherwise
    # take every section below it down as collateral, including the one line
    # that tells the reader what the file gives away.
    kept: list[str] = []
    size = 0
    dropped = False
    for line in parts:
        if size + len(line) + 1 > SAFE_LIMIT:
            dropped = True
            continue
        kept.append(line)
        size += len(line) + 1

    message = "\n".join(kept)
    if dropped:
        message += "\n\n" + t.get("bot.truncated")
    return message if len(message) <= MESSAGE_LIMIT else t.get("bot.truncated")


def render_tag_dump(report: Report) -> str:
    """Every raw tag, for the attachment the 'all tags' button sends."""
    lines = [
        f"findpic — {report.file.name}",
        f"sha256 {report.file.sha256 or '-'}",
        "=" * 60,
        "",
    ]
    for key in sorted(report.raw):
        lines.append(f"{key:<44} {report.raw[key]}")
    return "\n".join(lines)
