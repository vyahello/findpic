"""Analysis engine: file in, :class:`Report` out."""

from __future__ import annotations

from pathlib import Path

from ..container import scan as scan_container
from ..exif import ExifTool, Metadata
from ..geocode import Geocoder
from ..i18n import Translator
from ..jpegprint import MAX_HEADER_BYTES, embedded_thumbnail, fingerprint_bytes
from ..models import Report

# Importing the rule packs registers them. Keep this after the registry import.
from . import (
    rules_ai,  # noqa: F401,E402
    rules_authenticity,  # noqa: F401,E402
    rules_platform,  # noqa: F401,E402
    rules_privacy,  # noqa: F401,E402
    rules_recovery,  # noqa: F401,E402
    rules_structure,  # noqa: F401,E402
)
from .context import AnalysisOptions, Context
from .extract import (
    extract_capture,
    extract_device,
    extract_file,
    extract_image,
    extract_location,
    extract_people,
    resolve_place,
)
from .registry import all_rules, run_rules
from .verdict import build_verdicts

__all__ = [
    "AnalysisOptions",
    "Context",
    "analyze",
    "analyze_metadata",
    "all_rules",
]


def _fingerprint(context: Context, path: Path) -> None:
    """Read the compression structure, and the preview's separately.

    Bounded by design: only the header is read, and nothing is decoded, so this
    costs a few seeks even on a file that claims to be enormous. A file we
    cannot open is not an error here — the rest of the report still stands.
    """
    try:
        header = path.open("rb").read(MAX_HEADER_BYTES)
    except OSError:
        return
    context.jpeg = fingerprint_bytes(header)
    preview = embedded_thumbnail(header)
    if preview:
        context.thumbnail = fingerprint_bytes(preview)


def analyze(
    path: str | Path,
    exiftool: ExifTool | None = None,
    geocoder: Geocoder | None = None,
    options: AnalysisOptions | None = None,
    translator: Translator | None = None,
) -> Report:
    """Read and analyse one image file."""
    options = options or AnalysisOptions()
    exiftool = exiftool or ExifTool()
    metadata = exiftool.read(path)
    return analyze_metadata(
        Path(path),
        metadata,
        geocoder=geocoder,
        options=options,
        translator=translator,
    )


def analyze_metadata(
    path: Path,
    metadata: Metadata,
    geocoder: Geocoder | None = None,
    options: AnalysisOptions | None = None,
    translator: Translator | None = None,
) -> Report:
    """Analyse already-extracted metadata.

    Split out from :func:`analyze` so callers that already hold a
    :class:`Metadata` — tests, and the Telegram bot's batch path — do not pay for
    a second exiftool run.
    """
    options = options or AnalysisOptions()
    translator = translator or Translator(options.language)

    context = Context(meta=metadata, path=path, options=options)
    context.file = extract_file(path, metadata, options)
    context.image = extract_image(metadata)
    context.device = extract_device(metadata)
    context.capture = extract_capture(metadata)
    context.location = extract_location(metadata)
    context.people = extract_people(metadata)

    if options.scan_container:
        context.container = scan_container(path)

    if options.fingerprint_encoder:
        _fingerprint(context, path)

    if options.geocode and context.location.present:
        active = geocoder or Geocoder(enabled=True, language=options.language)
        resolve_place(context.location, active)
        active.save_cache()

    findings = list(run_rules(context))

    return Report(
        file=context.file,
        image=context.image,
        device=context.device,
        capture=context.capture,
        location=context.location,
        people=context.people,
        findings=findings,
        verdicts=build_verdicts(context, findings),
        tag_count=metadata.tag_count,
        groups=metadata.group_counts(),
        exiftool_warnings=metadata.warnings,
        errors=metadata.errors,
        raw=dict(metadata.human),
        translator=translator,
    )
