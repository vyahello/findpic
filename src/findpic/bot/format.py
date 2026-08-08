"""Render a :class:`Report` as a Telegram message.

Telegram is not a terminal: 4096 characters per message, HTML rather than ANSI,
and a reader who is usually on a phone. So the shape differs from the CLI even
though the content is the same — verdicts as coloured dots, the essentials in
the body, and the long tail folded into an expandable quote the reader opens
only if they want it.

Everything interpolated here is escaped. Metadata is attacker-controlled: a
filename or a caption can contain ``<`` and would otherwise break the markup or
inject links.
"""

from __future__ import annotations

from html import escape

from ..i18n import Translator
from ..models import Category, Report, Severity, VerdictLevel
from ..util import parse_exif_datetime

#: Telegram's hard limit. We aim well below it and fold the rest away.
MESSAGE_LIMIT = 4096
SAFE_LIMIT = 3600

LEVEL_DOT: dict[VerdictLevel, str] = {
    VerdictLevel.GOOD: "🟢",
    VerdictLevel.FAIR: "🟡",
    VerdictLevel.POOR: "🟠",
    VerdictLevel.BAD: "🔴",
    VerdictLevel.UNKNOWN: "⚪",
}

SEVERITY_MARK: dict[Severity, str] = {
    Severity.INFO: "·",
    Severity.NOTICE: "•",
    Severity.WARNING: "⚠️",
    Severity.CRITICAL: "🔴",
}

AXES = ("originality", "privacy", "structure")


def esc(value: object) -> str:
    """HTML-escape anything before it reaches a Telegram message."""
    return escape(str(value), quote=False)


def _section(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    return ["", f"<b>{esc(title)}</b>", *lines]


def render_verdicts(report: Report) -> list[str]:
    t = report.translator
    lines: list[str] = []
    for axis in AXES:
        verdict = report.verdicts.get(axis)
        if verdict is None:
            continue
        lines.append(
            f"{LEVEL_DOT[verdict.level]} <b>{esc(verdict.label(t))}</b> — "
            f"{esc(t.get(f'ui.axis.{axis}')).lower()}"
        )
    return lines


def render_device(report: Report) -> list[str]:
    t, device = report.translator, report.device
    if not (device.make or device.model):
        return []

    head = esc(device.label)
    if device.os:
        head = f"{head} · {esc(device.os)}"
    lines = [head]
    if device.lens_model:
        lines.append(f"<i>{esc(device.lens_model)}</i>")
    if device.capture_mode_keys:
        modes = " · ".join(t.get(f"mode.{key}") for key in device.capture_mode_keys)
        lines.append(esc(modes))
    if device.editor:
        lines.append(f"✏️ {esc(device.editor)}")
    return _section(t.get("ui.section.device"), lines)


def render_when(report: Report) -> list[str]:
    t, capture = report.translator, report.capture
    if not capture.taken:
        return []
    when = t.describe_when(parse_exif_datetime(capture.taken))
    line = esc(capture.taken)
    if when:
        line = f"{line}  <i>({esc(when)})</i>"
    lines = [line]
    if capture.modified and capture.modified_matches_taken is False:
        lines.append(f"✏️ {esc(t.get('ui.label.modified'))}: {esc(capture.modified)}")
    return _section(t.get("ui.section.when"), lines)


def render_where(report: Report) -> list[str]:
    t, location = report.translator, report.location
    if not location.present:
        return []

    lines: list[str] = []
    if location.place:
        lines.append(f"<b>{esc(location.place)}</b>")
    coordinates = f"<code>{esc(location.decimal)}</code>"
    if location.accuracy_m:
        accuracy = t.get("ui.unit.metres", value=f"{location.accuracy_m:g}")
        coordinates = f"{coordinates}  ±{esc(accuracy)}"
    lines.append(coordinates)
    if location.osm_url:
        lines.append(f'<a href="{esc(location.osm_url)}">{esc(t.get("ui.value.open_map"))}</a>')
    return _section(t.get("ui.section.where"), lines)


def render_image(report: Report) -> list[str]:
    t, image, capture = report.translator, report.image, report.capture
    parts: list[str] = []
    if image.dimensions:
        size = image.dimensions
        if image.megapixels:
            size = t.get("ui.value.dimensions", size=size, megapixels=f"{image.megapixels:.1f}")
        parts.append(size)
    exposure = " · ".join(
        piece
        for piece in (
            f"ISO {capture.iso}" if capture.iso else None,
            f"f/{capture.f_number:g}" if capture.f_number else None,
            f"{capture.exposure_time}s" if capture.exposure_time else None,
        )
        if piece
    )
    if exposure:
        parts.append(exposure)
    return _section(t.get("ui.section.image"), [esc(p) for p in parts])


def _finding_lines(report: Report, categories: tuple[Category, ...]) -> list[str]:
    t = report.translator
    lines: list[str] = []
    for finding in report.sorted_findings:
        if finding.category not in categories:
            continue
        if finding.severity is Severity.INFO:
            continue
        lines.append(f"{SEVERITY_MARK[finding.severity]} {esc(finding.title(t))}")
    return lines


def render_findings(report: Report) -> list[str]:
    """The things the reader should act on, with the rest folded away."""
    t = report.translator
    lines: list[str] = []

    urgent = _finding_lines(report, (Category.STRUCTURAL,))
    if urgent:
        lines += _section(t.get("ui.category.structural"), urgent)

    privacy = _finding_lines(report, (Category.PRIVACY,))
    if privacy:
        lines += _section(t.get("ui.category.privacy"), privacy)

    originality = _finding_lines(report, (Category.AUTHENTICITY, Category.AI))
    if originality:
        lines += _section(t.get("ui.category.authenticity"), originality)

    return lines


def render_details(report: Report) -> str:
    """The full explanation of every finding, for the expandable quote."""
    t = report.translator
    blocks: list[str] = []
    for finding in report.sorted_findings:
        if finding.severity is Severity.INFO:
            continue
        title = esc(finding.title(t))
        detail = esc(finding.detail(t))
        block = f"<b>{title}</b>"
        if detail:
            block += f"\n{detail}"
        blocks.append(block)
    return "\n\n".join(blocks)


def render_report(report: Report, *, source_note: str = "") -> str:
    """Build the complete message body."""
    t = report.translator

    header = [
        f"🔎 <b>{esc(report.file.name)}</b>",
        f"<i>{esc(report.file.file_type or '?')} · "
        f"{esc(t.bytes(report.file.size_bytes))} · "
        f"{esc(t.get('ui.header.tags', report.tag_count))}</i>",
        "",
        *render_verdicts(report),
    ]
    if source_note:
        header += ["", source_note]

    body = (
        render_device(report)
        + render_when(report)
        + render_where(report)
        + render_image(report)
        + render_findings(report)
    )

    message = "\n".join(header + body)

    details = render_details(report)
    if details:
        # An expandable quote keeps the message short while leaving every
        # explanation one tap away.
        candidate = f"{message}\n\n<blockquote expandable>{details}</blockquote>"
        if len(candidate) <= SAFE_LIMIT:
            return candidate

    if len(message) > MESSAGE_LIMIT:
        message = message[: MESSAGE_LIMIT - 1] + "…"
    return message


def render_tag_dump(report: Report) -> str:
    """Every raw tag, for the attachment the 'all tags' button sends."""
    lines = [
        f"findpic — {report.file.name}",
        f"sha256 {report.file.sha256 or '-'}",
        "=" * 60,
        "",
    ]
    for key in sorted(report.raw):
        value = report.raw[key]
        lines.append(f"{key:<44} {value}")
    return "\n".join(lines)


def quota_footer(translator: Translator, used: int, limit: int) -> str:
    if not limit:
        return ""
    return f"\n\n<i>{esc(translator.get('bot.quota.footer', used=used, limit=limit))}</i>"
