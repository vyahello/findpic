"""Rules that judge whether the file is an untouched camera original.

The bar for calling something "modified" is deliberately high. Plenty of
innocent pipelines (a cloud backup, an OS-level rotate) touch a file without any
intent to deceive, so each rule reports what it observed and lets the reader draw
the conclusion.

Rules produce facts, never sentences — see :class:`findpic.models.Finding`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from ..models import Category, Confidence, Finding, Severity
from ..tables import RETOUCH_EDITORS, match_jpeg_digest
from ..util import compare_geometry, parse_exif_datetime, truncate
from .context import Context
from .registry import rule


@rule("editor_software", Category.AUTHENTICITY, order=10)
def editor_software(context: Context) -> Iterable[Finding]:
    """The Software tag naming a known editing application."""
    editor = context.device.editor
    if not editor:
        return
    retouched = editor in RETOUCH_EDITORS
    yield Finding(
        id="authenticity.editor_software",
        category=Category.AUTHENTICITY,
        severity=Severity.WARNING if retouched else Severity.NOTICE,
        confidence=Confidence.HIGH,
        params={"editor": editor},
        detail_variant="retouch" if retouched else "reencode",
        evidence={"Software": context.device.software_raw},
        weight=40 if retouched else 25,
    )


@rule("jpeg_digest", Category.AUTHENTICITY, order=12)
def jpeg_digest(context: Context) -> Iterable[Finding]:
    """Identify the encoder from its JPEG quantization-table fingerprint.

    Every JPEG encoder ships its own compression tables, and exiftool carries a
    database of their fingerprints. When the tables belong to desktop editing
    software but the metadata claims a camera, the file was re-saved — this holds
    even if every Exif tag was left pristine, which makes it one of the few
    authenticity signals that is awkward to forge.
    """
    digest = context.meta.str("File:JPEGDigest")
    if not digest or not context.has_camera_identity:
        return
    matched = match_jpeg_digest(digest)
    if matched is None:
        return
    software, variant = matched
    yield Finding(
        id="authenticity.jpeg_digest",
        category=Category.AUTHENTICITY,
        severity=Severity.WARNING,
        confidence=Confidence.MEDIUM if variant == "editor" else Confidence.LOW,
        variant=variant,
        detail_variant=variant,
        params={
            "software": software,
            "device": context.device.label,
            "quality": context.meta.int("File:JPEGQualityEstimate") or "?",
        },
        evidence={
            "JPEGDigest": digest,
            "JPEGQualityEstimate": context.meta.get("File:JPEGQualityEstimate"),
        },
        weight=30 if variant == "editor" else 18,
    )


@rule("xmp_edit_history", Category.AUTHENTICITY, order=11)
def xmp_edit_history(context: Context) -> Iterable[Finding]:
    """Adobe's XMP media-management block records an editing lineage."""
    meta = context.meta
    history = meta.get("XMP-xmpMM:History", "History")
    derived = meta.get("XMP-xmpMM:DerivedFrom", "DerivedFrom")

    if history:
        entries = history if isinstance(history, list) else [history]
        yield Finding(
            id="authenticity.xmp_history",
            category=Category.AUTHENTICITY,
            severity=Severity.WARNING,
            confidence=Confidence.HIGH,
            count=len(entries),
            evidence={"XMP-xmpMM:History": truncate(history, 300)},
            weight=20,
        )
    if derived:
        yield Finding(
            id="authenticity.derived_from",
            category=Category.AUTHENTICITY,
            severity=Severity.WARNING,
            confidence=Confidence.HIGH,
            evidence={"XMP-xmpMM:DerivedFrom": truncate(derived, 200)},
            weight=25,
        )


@rule("modify_date_differs", Category.AUTHENTICITY, order=20)
def modify_date_differs(context: Context) -> Iterable[Finding]:
    """A ModifyDate that is not the capture time means the file was re-saved."""
    capture = context.capture
    if capture.modified_matches_taken is not False:
        return
    yield Finding(
        id="authenticity.modify_date_differs",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.HIGH,
        params={"taken": capture.taken, "modified": capture.modified},
        evidence={"DateTimeOriginal": capture.taken, "ModifyDate": capture.modified},
        weight=15,
    )


@rule("dimension_mismatch", Category.AUTHENTICITY, order=21)
def dimension_mismatch(context: Context) -> Iterable[Finding]:
    """Exif remembers the original size; the JPEG header reports the current one."""
    meta = context.meta
    exif_width = meta.int("ExifIFD:ExifImageWidth")
    exif_height = meta.int("ExifIFD:ExifImageHeight")
    real_width = meta.int("File:ImageWidth")
    real_height = meta.int("File:ImageHeight")
    change = compare_geometry((exif_width, exif_height), (real_width, real_height))
    # None means they agree; "rotate" is a lossless orientation swap, not an edit.
    if change in (None, "rotate"):
        return
    resized = change == "resize"

    yield Finding(
        id="authenticity.dimension_mismatch",
        category=Category.AUTHENTICITY,
        severity=Severity.WARNING,
        confidence=Confidence.HIGH,
        variant="resize" if resized else "crop",
        params={
            "exif_width": exif_width,
            "exif_height": exif_height,
            "real_width": real_width,
            "real_height": real_height,
        },
        evidence={
            "ExifImageWidth": exif_width,
            "ExifImageHeight": exif_height,
            "ImageWidth": real_width,
            "ImageHeight": real_height,
        },
        weight=20,
    )


@rule("adobe_resave", Category.AUTHENTICITY, order=22)
def adobe_resave(context: Context) -> Iterable[Finding]:
    """The APP14 'Adobe' segment is stamped by Adobe's JPEG encoder."""
    meta = context.meta
    if not meta.has_group("Adobe") and not meta.has("Adobe:DCTEncodeVersion"):
        return
    yield Finding(
        id="authenticity.adobe_segment",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        evidence={"Adobe": meta.get("Adobe:DCTEncodeVersion", "Adobe")},
        weight=15,
    )


@rule("progressive_jpeg", Category.AUTHENTICITY, order=23)
def progressive_jpeg(context: Context) -> Iterable[Finding]:
    """Cameras write baseline JPEG; progressive means a web pipeline touched it."""
    encoding = (context.image.encoding_process or "").lower()
    if "progressive" not in encoding or not context.has_camera_identity:
        return
    yield Finding(
        id="authenticity.progressive_jpeg",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        evidence={"EncodingProcess": context.image.encoding_process},
        weight=12,
    )


@rule("camera_without_makernotes", Category.AUTHENTICITY, order=30)
def camera_without_makernotes(context: Context) -> Iterable[Finding]:
    """A named camera with no MakerNote block is a re-save or a rewrite.

    Only for files that have an Exif block at all. An MP4 or a MOV names its
    camera in QuickTime atoms and has no ExifIFD to have lost — accusing it of
    a re-save is accusing it of losing something it never had a place to keep.
    """
    device = context.device
    if not context.has_exif or not context.has_camera_identity or device.has_makernotes:
        return
    yield Finding(
        id="authenticity.no_makernotes",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        params={"device": device.label},
        evidence={"Make": device.make, "Model": device.model},
        weight=8,
    )


@rule("makernotes_intact", Category.AUTHENTICITY, order=31)
def makernotes_intact(context: Context) -> Iterable[Finding]:
    """Positive evidence — worth stating plainly, and it carries no weight."""
    device = context.device
    if not device.has_makernotes:
        return
    yield Finding(
        id="authenticity.makernotes_intact",
        category=Category.AUTHENTICITY,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        params={"vendor": device.makernote_vendor},
        evidence={"MakerNote": device.makernote_vendor},
        weight=0,
    )


@rule("no_exif_at_all", Category.AUTHENTICITY, order=40)
def no_exif_at_all(context: Context) -> Iterable[Finding]:
    """No Exif block at all — informative, never incriminating."""
    if context.has_exif or context.has_camera_identity:
        return
    yield Finding(
        id="authenticity.no_exif",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.HIGH,
        evidence={"tag_count": context.meta.tag_count},
        weight=0,
    )


@rule("thin_camera_claim", Category.AUTHENTICITY, order=41)
def thin_camera_claim(context: Context) -> Iterable[Finding]:
    """Modern devices write timing and lens detail; a bare claim is suspicious.

    Every probe below is an ``ExifIFD:`` tag, so this can only mean anything
    about a file that has an ExifIFD. A video has none, and used to collect two
    accusations for it.
    """
    device = context.device
    meta = context.meta
    if (
        not context.has_exif
        or not context.has_camera_identity
        or device.has_makernotes
        or device.editor
    ):
        return

    missing = [
        key
        for key, present in (
            ("subsec", meta.has("ExifIFD:SubSecTimeOriginal")),
            ("offsets", meta.has("ExifIFD:OffsetTimeOriginal", "ExifIFD:OffsetTime")),
            ("lens", meta.has("ExifIFD:LensModel", "ExifIFD:LensInfo")),
            ("exposure", meta.has("ExifIFD:ExposureTime", "ExifIFD:FNumber")),
        )
        if not present
    ]
    if len(missing) < 3:
        return
    yield Finding(
        id="authenticity.thin_camera_claim",
        category=Category.AUTHENTICITY,
        severity=Severity.WARNING,
        confidence=Confidence.LOW,
        params={"device": device.label, "missing_keys": missing},
        evidence={"Make": device.make, "Model": device.model, "missing": missing},
        weight=15,
    )


@rule("gps_time_disagrees", Category.AUTHENTICITY, order=42)
def gps_time_disagrees(context: Context) -> Iterable[Finding]:
    """The GPS clock is UTC straight off the satellites and hard to fake."""
    capture = context.capture
    gps_date = context.meta.str("GPS:GPSDateStamp")
    if not (capture.taken and gps_date):
        return
    taken = parse_exif_datetime(capture.taken)
    gps_day = _as_date(gps_date.replace(":", "-"))
    if taken is None or gps_day is None:
        return
    # A genuine capture near midnight lands on a different UTC day, so only a gap
    # bigger than that is evidence of anything.
    gap = abs((taken.date() - gps_day).days)
    if gap <= 1:
        return

    # A phone that could not see satellites falls back to a cell or wifi fix and
    # carries the timestamp of the last real one. That is ordinary, and it used
    # to be enough on its own to declare the whole file forged: this rule is the
    # only member of CONTRADICTION_IDS, which short-circuits every other signal
    # and lands the verdict on "the metadata contradicts itself — do not trust
    # it". A four-day-old cached fix should not do that.
    #
    # Two conditions, both required, and the discriminators were already being
    # extracted and never used. The GPS clock must be *behind* the shutter (a
    # cached fix can only be older, never newer), and the recorded accuracy or
    # the processing method must say this was not a satellite lock.
    accuracy = context.location.accuracy_m
    method = (context.location.processing_method or "").upper()
    stale = gps_day < taken.date() and (
        (accuracy is not None and accuracy > 30) or (method and "GPS" not in method)
    )
    if stale:
        yield Finding(
            id="authenticity.gps_fix_is_stale",
            category=Category.AUTHENTICITY,
            severity=Severity.NOTICE,
            confidence=Confidence.MEDIUM,
            params={
                "gap": gap,
                "gps_date": gps_day,
                "accuracy": f"{accuracy:g}" if accuracy is not None else method.lower(),
            },
            evidence={
                "DateTimeOriginal": capture.taken,
                "GPSDateStamp": gps_date,
                "GPSHPositioningError": accuracy,
                "GPSProcessingMethod": context.location.processing_method,
            },
            weight=5,
        )
        return

    # No accuracy and no method recorded is the common case on Apple files. Fall
    # back to the old behaviour rather than going silent: a real forgery must
    # still be caught, and absence of the discriminator is not absence of the
    # problem.
    yield Finding(
        id="authenticity.gps_time_disagrees",
        category=Category.AUTHENTICITY,
        severity=Severity.WARNING,
        confidence=Confidence.MEDIUM,
        params={"gap": gap, "camera_date": taken.date(), "gps_date": gps_day},
        evidence={"DateTimeOriginal": capture.taken, "GPSDateStamp": gps_date},
        weight=35,
    )


def _as_date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text)
    except (ValueError, TypeError):
        return None
