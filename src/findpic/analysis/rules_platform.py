"""Rules about where a file has been before it reached you.

Messengers and social platforms re-encode uploads and throw away metadata. That
leaves a recognisable shape: no MakerNotes, no Exif, a capped dimension, often a
telltale filename. None of these are proof on their own, so findings here stay
low-confidence and carry no verdict weight — they are context, not accusations.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import Category, Confidence, Finding, Severity
from ..tables import match_filename
from .context import Context
from .registry import rule

#: Longest-edge caps that messaging and social platforms resize down to.
KNOWN_CAPS: dict[int, str] = {
    1280: "Telegram (sent as a photo), WhatsApp",
    1600: "Facebook, WhatsApp HD",
    2048: "Facebook, Twitter/X",
    1440: "Instagram",
    1080: "Instagram, Snapchat",
    960: "older messenger compression",
    2560: "Telegram (high quality)",
}


@rule("filename_origin", Category.PLATFORM, order=1)
def filename_origin(context: Context) -> Iterable[Finding]:
    """The filename as a hint about where the file has been."""
    matched = match_filename(context.file.name)
    if not matched:
        return
    yield Finding(
        id="platform.filename_hint",
        category=Category.PLATFORM,
        severity=Severity.INFO,
        confidence=Confidence.LOW,
        params={"source_keys": [matched], "note_keys": [matched]},
        evidence={"filename": context.file.name},
    )


@rule("stripped_by_pipeline", Category.PLATFORM, order=2)
def stripped_by_pipeline(context: Context) -> Iterable[Finding]:
    """The characteristic shape of a file that went through an upload pipeline."""
    meta = context.meta
    if context.has_camera_identity or context.device.has_makernotes:
        return
    if meta.tag_count > 40:
        return

    image = context.image
    longest = max(image.width or 0, image.height or 0)
    cap_note = KNOWN_CAPS.get(longest)

    yield Finding(
        id="platform.stripped",
        category=Category.PLATFORM,
        severity=Severity.INFO,
        confidence=Confidence.MEDIUM if cap_note else Confidence.LOW,
        variant="known" if cap_note else "unknown",
        detail_variant="known" if cap_note else None,
        params={"longest": longest, "platforms": cap_note or ""},
        evidence={"tag_count": meta.tag_count, "longest_edge": longest or None},
    )


@rule("editor_pipeline_note", Category.PLATFORM, order=3)
def editor_pipeline_note(context: Context) -> Iterable[Finding]:
    """A camera identity that survived alongside an editor is worth calling out."""
    device = context.device
    if not (device.editor and context.has_camera_identity):
        return
    yield Finding(
        id="platform.camera_and_editor",
        category=Category.PLATFORM,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        params={"editor": device.editor, "device": device.label},
        evidence={"Model": device.model, "Software": device.software_raw},
    )
