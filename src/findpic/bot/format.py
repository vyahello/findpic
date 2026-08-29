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
from ..models import Category, Confidence, Finding, Report, Severity, VerdictLevel
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
    # The one place a stripped file gets a vendor attribution on screen: the
    # filename shape survives what the tags did not.
    "platform.filename_hint",
)

#: Findings that explain, before the reader meets a verdict shaped like failure,
#: why there is nothing in the file to report. They go above the headline, and
#: they are exempt from the INFO filter every other section applies — the whole
#: point is that they are the most important line in the message.
PROVENANCE_FINDINGS = ("recovery.screenshot", "platform.stripped")

EXPOSURE_MARK: dict[VerdictLevel, str] = {
    VerdictLevel.GOOD: "🟢",
    VerdictLevel.FAIR: "🟡",
    VerdictLevel.POOR: "🟠",
    VerdictLevel.BAD: "🔴",
}

#: How a finding's own uncertainty is spoken. HIGH says nothing — a hedge on
#: everything is a hedge on nothing.
HEDGE = {Confidence.MEDIUM: "bot.hedge.medium", Confidence.LOW: "bot.hedge.low"}

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
    """Make a metadata value safe to put in a message.

    Escaping the markup is not enough on its own. A newline inside a tag value
    survives it, and every line of this report is a claim the bot is making —
    so a camera model containing "\n\n📍 WHERE\nBuckingham Palace" adds lines
    to the report that the reader has no way to tell from the bot's own. The
    tags would not render, but the sentences would.

    So line structure belongs to the renderer and never to the data: newlines
    and other control whitespace collapse to a space, the way the terminal
    report's own sanitiser has always treated them.
    """
    text = str(value)
    if any(character in text for character in "\n\r\t\v\f"):
        text = " ".join(text.split())
    return escape(text, quote=False)


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


def report_is_stripped(report: Report) -> bool:
    """Nothing in this file names a camera or a moment.

    One definition, used by the renderer to decide whether a verdict is worth
    showing and by the analytics to decide what to record. They were computing
    it separately, which meant the badge the reader saw and the statistic the
    admin read could disagree about the same file.
    """
    return not (report.device.make or report.device.model) and not report.capture.taken


def _hedged(finding: Finding, t: Translator, *, floor: Confidence = Confidence.MEDIUM) -> str:
    """A finding's title, carrying its own confidence.

    The bot attached none, so a MEDIUM guess and a HIGH certainty read
    identically — on a tool whose entire premise is that metadata lies.

    ``floor`` is what keeps that from becoming noise. A claim that justifies a
    verdict gets the full sentence at MEDIUM and below; inside a section already
    titled "what the file still shows", where every line is a reading of the
    compression, only a genuine guess is worth marking.
    """
    text = esc(finding.title(t))
    key = HEDGE.get(finding.confidence)
    if key is None or finding.confidence.rank > floor.rank:
        return text
    return t.get(key, text=text)


def _block(title: str, lines: list[str]) -> list[str]:
    lines = [line for line in lines if line]
    if not lines:
        return []
    return ["", f"<b>{esc(title)}</b>", *lines]


# ------------------------------------------------------------------ sections


def render_headline(report: Report, *, name: str = "", badge: bool = True) -> list[str]:
    """What the file is, and the one verdict worth stating up front.

    Dimensions are deliberately absent: THE SHOT already carries them, with
    megapixels and the aspect ratio beside them, and printing the same two
    numbers twice in two different spacings is exactly the habit this module
    exists to avoid.
    """
    t = report.translator
    heading = name or esc(report.file.name)
    lines = [
        f"🔎 <b>{heading}</b>",
        f"<i>{esc(f'{t.bytes(report.file.size_bytes)} · {report.file.file_type or chr(63)}')}</i>",
    ]

    verdict = report.verdicts.get("originality")
    if verdict is None or not badge:
        return lines

    lines += [
        "",
        f"{ORIGINALITY_MARK[verdict.level]} <b>{esc(verdict.label(t))}</b>",
        f"<i>{esc(t.get(f'bot.originality.{verdict.level.value}'))}</i>",
    ]
    # A verdict with no evidence is an opinion. UNKNOWN is excluded because its
    # reasons are recovery trivia, and GOOD because there is nothing to justify.
    if verdict.level not in (VerdictLevel.GOOD, VerdictLevel.UNKNOWN) and verdict.reasons:
        lines.append(f"<i>{esc(t.get('bot.verdict.because'))}</i>")
        lines += [f"• {_hedged(finding, t)}" for finding in verdict.reasons[:3]]
    return lines


def render_device(report: Report) -> list[str]:
    t, device = report.translator, report.device
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

    lines = [head]
    if second:
        lines.append(esc(" · ".join(second)))
    # The lens, which bot.help has always promised and this never showed. It is
    # also the string that resolves the two-focal-lengths confusion by itself:
    # "iPhone X back dual camera 4mm f/1.8".
    lens = (device.lens_model or "").strip()
    if lens and lens.lower() != device.label.lower():
        lines.append(f"📷 {esc(lens[:60])}")
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
        when = t.describe_when(moment)
        if when:
            lines.append(esc(when))
    else:
        lines.append(f"<b>{esc(capture.taken)}</b>")

    # The offset used to be withheld as meaningless, while the finding that says
    # "this records which band of the world you were in" was filtered out of the
    # leaks list as already shown here. It was shown nowhere. Said as what it
    # costs rather than as "UTC+02:00", it is worth the line.
    if capture.taken_offset:
        lines.append(esc(t.get("bot.when.offset", offset=capture.taken_offset)))

    if capture.modified and capture.modified_matches_taken is False:
        rewritten = parse_exif_datetime(capture.modified)
        value = _format_date(rewritten, t) if rewritten else capture.modified
        lines.append(f"✏️ {esc(t.get('bot.when.modified', value=value))}")
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


def render_provenance(report: Report) -> str:
    """The one line that has to come before a verdict shaped like failure.

    Two findings explain an empty report and neither ever reached a user: that
    the file came through a messenger, and that it is a screen capture rather
    than a photograph. Both were computed, both translated, both discarded —
    one because it is not in TRACE_FINDINGS, the other because render_traces
    gives up whenever a capture time survives, and screenshots keep theirs.

    Their severity is INFO, so this deliberately does not reuse the INFO filter
    every other section applies. On these files they are the most important
    sentence in the message.
    """
    t = report.translator
    found = {finding.id: finding for finding in report.findings}

    screenshot = found.get("recovery.screenshot")
    if screenshot is not None:
        return t.get("bot.note.screenshot")

    stripped = found.get("platform.stripped")
    if stripped is None or not report_is_stripped(report):
        return ""
    params = stripped.resolve_params(t)
    # The finding carries variant="known"/"unknown": with no matching resize
    # ceiling `platforms` is empty, and one string would read "the ceiling
    # resize to" with a hole in the middle.
    body = t.get(f"bot.note.stripped.{stripped.variant or 'unknown'}", **params)
    return "\n".join(
        (
            f"🫥 <b>{t.get('bot.note.stripped.title')}</b>",
            body,
            t.get("bot.note.stripped.deliberate"),
        )
    )


def render_traces(report: Report, *, skip: set[str] | None = None) -> list[str]:
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
    # A named camera means the reader has real answers and these would be
    # structural trivia buried under them. A capture time on its own does not:
    # that is a screenshot or a stripped file, exactly the case where the
    # compression is the only evidence there is.
    if report.device.make or report.device.model:
        return []
    skip = skip or set()
    lines = [
        f"• {_hedged(finding, t, floor=Confidence.LOW)}"
        for finding in report.sorted_findings
        if finding.id in TRACE_FINDINGS and finding.id not in skip
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
    elif location.geocode_error:
        # Otherwise a lookup that failed and a coordinate in the middle of the
        # sea render identically: bare numbers and no explanation. Nominatim's
        # own reason string is machine English and stays in the terminal report.
        lines.append(esc(t.get("bot.where.unresolved")))
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
        )
        if piece
    ]
    if exposure:
        lines.append(f"<code>{esc(' · '.join(exposure))}</code>")

    # One focal-length line, not two. DEVICE said "28 mm equiv." and THE SHOT
    # said "4 mm", four lines apart with nothing to connect them; to anyone who
    # is not a photographer that is a contradiction. The nesting keeps the unit
    # translatable — hard-coding "mm" here loses it in Ukrainian.
    focal = _focal_line(report)
    if focal:
        lines.append(esc(focal))

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


def _focal_line(report: Report) -> str | None:
    """The lens's reach, said once and in a way a non-photographer can read."""
    t, capture = report.translator, report.capture
    actual = t.get("detail.mm", value=f"{capture.focal_mm:g}") if capture.focal_mm else None
    equivalent = t.get("detail.mm", value=f"{capture.focal_35mm:g}") if capture.focal_35mm else None
    if actual and equivalent:
        return t.get("detail.focal_pair", actual=actual, equivalent=equivalent)
    if equivalent:
        return t.get("detail.equivalent", value=equivalent)
    return actual


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
        # A finding with a variant needs its variant's leak line, or a resize
        # keeps being described as a crop.
        stem = finding.id.split(".", 1)[1]
        key = f"bot.leak.{stem}.{finding.variant}" if finding.variant else f"bot.leak.{stem}"
        if not t.has(key):
            key = f"bot.leak.{stem}"
        label = t.get(key, **finding.resolve_params(t)) if t.has(key) else finding.title(t)
        items.append(f"• {esc(label)}")

    # The head comes from what survived the filter, not from the raw verdict.
    # Two of the owner's five samples score FAIR on a finding this section then
    # hides, and printing "LOW EXPOSURE" directly above "nothing worth removing"
    # is the message contradicting itself.
    level = report.verdicts["privacy"].level if items else VerdictLevel.GOOD
    if level not in EXPOSURE_MARK:  # privacy_verdict never returns UNKNOWN
        level = VerdictLevel.GOOD
    head = [
        f"{EXPOSURE_MARK[level]} <b>{esc(t.get(f'verdict.privacy.{level.value}.label'))}</b>",
        f"<i>{esc(t.get(f'bot.exposure.{level.value}'))}</i>",
    ]
    # Never vanish when there is nothing to list. "This file is safe to forward"
    # and "I did not look" read identically when the section is simply absent —
    # and the head alone says it, so no bullet repeats it.
    return _block(t.get("bot.section.leaks"), head + items)


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


def render_report(
    report: Report, *, source_note: str = "", name: str = "", footer: str = ""
) -> str:
    """Build the complete message body.

    Two orderings matter here and both were wrong.

    The lead note goes *above* the headline. It exists to explain why the report
    is empty, and printing it under a "❔ UNKNOWN" badge means the reader meets
    the failure first and the explanation second — which is the arrangement the
    module docstring has always said it was avoiding.

    And what the file gives away goes above the photographic detail. ISO and
    aperture are the least actionable lines in the message; the exposure block
    is the one that changes what the reader does next, and it was sitting on
    line 34 of 35, below the colour profile.
    """
    t = report.translator
    provenance = render_provenance(report)
    lead = source_note or provenance

    # A verdict printed under a note that has just explained why the file has
    # nothing to judge is the same failure said twice, and the badge is the
    # discouraging half. Suppressed for a provenance note — which covers the
    # screenshot, whose surviving capture time would otherwise keep the badge
    # alive — and for a compressed photo that arrived with nothing left.
    badge = not (provenance or (source_note and report_is_stripped(report)))

    parts: list[str] = []
    if lead:
        parts += [lead, ""]
    parts += render_headline(report, name=name, badge=badge)

    # Whatever the headline already justified must not be repeated as a trace:
    # on a stripped file the verdict's reasons and the traces list are the same
    # findings, and saying each thing once is the rule this module runs on.
    shown = set()
    verdict = report.verdicts.get("originality")
    if badge and verdict and verdict.level not in (VerdictLevel.GOOD, VerdictLevel.UNKNOWN):
        shown = {finding.id for finding in verdict.reasons[:3]}

    parts += (
        render_warnings(report)
        + render_device(report)
        + render_when(report)
        + render_where(report)
        + render_exposure_risks(report)
        + render_shot(report)
        + render_traces(report, skip=shown)
    )
    if footer:
        parts += ["", footer]

    return _fit(parts, t)


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
