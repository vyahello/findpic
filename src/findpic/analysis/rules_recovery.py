"""What can still be said about a file whose metadata is gone.

Every other rule pack reads tags. These read the compression, because on a photo
that came back from a messenger the tags are the part that did not survive.

The distinction they exist to draw is between two things that look identical in
a report and are not remotely the same event:

**Re-compressed.** The pixels were decoded and encoded again. Detail is gone for
good, and whatever wrote the new file chose new quantization tables — normally
the standard library ones, which name themselves exactly.

**Re-containerised.** The compressed data was copied through untouched and only
the wrapper was rebuilt. The picture is bit-for-bit the camera's; the metadata
around it is not. This is what a metadata stripper does, and reporting it as a
re-encode would tell the reader their picture had been degraded when it has not.

The tables tell them apart, because they belong to the compressed data rather
than to the container. A file carrying a camera's tables inside a library's
wrapper was re-containerised, and that is a statement about what happened to it
after the shutter closed that no surviving tag could have made.

None of this recovers anything. A deleted timestamp stays deleted. Every finding
here is attribution, and the catalogue text says so in as many words.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import Category, Confidence, Finding, Severity
from ..recover import PRECISION_SECOND, timestamp_from_filename
from ..tables import match_filename
from .context import Context
from .registry import rule

#: How far two aspect ratios may drift before we call them different shapes.
#: A preview is a rounded-down scaling of its parent, so 4032x3024 becomes
#: 160x120 exactly but 1200x1601 would not — a few percent of slack absorbs the
#: rounding without absorbing a real crop.
ASPECT_TOLERANCE = 0.04


def _library_shaped(context: Context) -> bool:
    """Whether the wrapper — not the compression — was written by a library.

    A library writer announces itself three ways at once: a JFIF header the
    camera did not write, one segment per table instead of one segment holding
    all of them, and the tables placed after the frame header rather than before
    it. Any one alone would be thin; together they are a different writer.
    """
    print_ = context.jpeg
    if print_ is None:
        return False
    return (
        "JFIF" in print_.app_identifiers
        and print_.dht_segments > 1
        and not print_.tables_before_frame
    )


def _aspect(width: int | None, height: int | None) -> float | None:
    if not width or not height:
        return None
    return width / height


@rule("encoder_family", Category.AUTHENTICITY, order=13)
def encoder_family(context: Context) -> Iterable[Finding]:
    """Name the encoder family from the quantization tables themselves.

    Only for files with nothing left to identify them. When a camera is named,
    ``jpeg_digest`` already speaks and two rules saying the same thing in
    different words is worse than one.
    """
    print_ = context.jpeg
    if print_ is None or not print_.ok or not print_.quant:
        return
    if context.has_camera_identity:
        return

    if print_.uses_library_tables:
        yield Finding(
            id="recovery.encoder_library",
            category=Category.AUTHENTICITY,
            severity=Severity.NOTICE,
            confidence=Confidence.HIGH,
            params={"quality": print_.ijg_quality},
            evidence={
                "quantization": "scaled ITU-T T.81 Annex K tables",
                "quality": print_.ijg_quality,
                "layout": print_.layout,
            },
            weight=10,
        )
        return

    luma = print_.luma
    if luma is None or not luma.repeats_first_row:
        return
    if _library_shaped(context):
        # container_rewritten is about to say this and more; two findings that
        # open with the same sentence read as the tool repeating itself.
        return
    yield Finding(
        id="recovery.encoder_vendor",
        category=Category.AUTHENTICITY,
        severity=Severity.INFO,
        confidence=Confidence.MEDIUM,
        params={},
        evidence={
            "quantization": "custom tables, first two rows identical",
            "table_signature": print_.table_signature,
            "restart_interval": print_.restart_interval,
            "layout": print_.layout,
        },
    )


@rule("container_rewritten", Category.AUTHENTICITY, order=14)
def container_rewritten(context: Context) -> Iterable[Finding]:
    """A camera's compressed data inside somebody else's wrapper.

    The tables came from the device that took the picture; the segment order and
    the JFIF header did not. Nothing re-compressed the image — it was unwrapped
    and wrapped again, which is what stripping metadata requires and what
    degrading the picture does not.

    Worth saying plainly, because the reader's real question is "has my photo
    been damaged", and here the answer is no even though the file has clearly
    been through something.
    """
    print_ = context.jpeg
    if print_ is None or not print_.ok or not print_.quant:
        return
    luma = print_.luma
    if luma is None or not luma.repeats_first_row or print_.uses_library_tables:
        return

    if not _library_shaped(context):
        return

    yield Finding(
        id="recovery.container_rewritten",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        params={},
        evidence={
            "table_signature": print_.table_signature,
            "layout": print_.layout,
            "dqt_segments": print_.dqt_segments,
            "dht_segments": print_.dht_segments,
        },
        weight=8,
    )


@rule("preview_survived_re_encode", Category.AUTHENTICITY, order=15)
def preview_survived_re_encode(context: Context) -> Iterable[Finding]:
    """The preview is older than the picture it sits next to.

    A camera writes both at once, so both carry that camera's tables. When the
    main image has been re-compressed by a library and the preview has not, the
    preview is a survivor from before the re-encode — the last piece of the
    original file still in the file.

    It is small, but it is the camera's own rendering, and its tables are
    evidence about the device when the main image's no longer are.
    """
    main, preview = context.jpeg, context.thumbnail
    if main is None or preview is None or not (main.ok and preview.ok):
        return
    preview_luma = preview.luma
    if preview_luma is None or not preview_luma.repeats_first_row:
        return

    # Two ways the preview can be older than the picture beside it: the picture
    # was re-compressed by a library and the preview was not, or the picture's
    # container was rebuilt and the preview was carried through as an opaque
    # blob — which shows up as the two disagreeing about where the tables go.
    re_encoded = main.uses_library_tables
    repackaged = main.tables_before_frame != preview.tables_before_frame
    if not (re_encoded or repackaged):
        return

    yield Finding(
        id="recovery.preview_older",
        variant="re_encoded" if re_encoded else "repackaged",
        detail_variant="re_encoded" if re_encoded else "repackaged",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        params={"quality": main.ijg_quality or "?"},
        evidence={
            "main_layout": main.layout,
            "preview_layout": preview.layout,
            "main_tables": f"library, quality {main.ijg_quality}",
            "preview_tables": preview.table_signature,
            "preview_size": f"{preview.width}x{preview.height}",
        },
    )


@rule("preview_shape_differs", Category.AUTHENTICITY, order=16)
def preview_shape_differs(context: Context) -> Iterable[Finding]:
    """The preview is a different shape from the picture.

    A preview is written once and then tends to be carried along. Crop the image
    with something that does not regenerate it and the preview keeps the
    original framing — the case where a redacted photograph still contains what
    was redacted, at 160 pixels wide.

    Reported as a shape difference rather than as "this was cropped", because
    the other explanation is a tool that wrote a lazy preview, and the file
    cannot distinguish them. Where it matters, the reader can simply look.
    """
    main, preview = context.jpeg, context.thumbnail
    if main is None or preview is None or not (main.ok and preview.ok):
        return
    main_aspect = _aspect(main.width, main.height)
    preview_aspect = _aspect(preview.width, preview.height)
    if main_aspect is None or preview_aspect is None:
        return
    # Compare shape, not orientation: a preview stored the other way round is a
    # rotation, which is a different and much duller story.
    if abs(main_aspect - preview_aspect) < ASPECT_TOLERANCE:
        return
    if abs(main_aspect - 1 / preview_aspect) < ASPECT_TOLERANCE:
        return

    yield Finding(
        id="recovery.preview_shape",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        params={
            "image": f"{main.width}×{main.height}",
            "preview": f"{preview.width}×{preview.height}",
        },
        evidence={
            "image_aspect": round(main_aspect, 4),
            "preview_aspect": round(preview_aspect, 4),
        },
        remediation="exiftool -b -ThumbnailImage photo.jpg > preview.jpg",
        weight=12,
    )


@rule("filename_timestamp", Category.PLATFORM, order=17)
def filename_timestamp(context: Context) -> Iterable[Finding]:
    """A capture time that outlived the tags, in the filename.

    Only worth saying when the file has no capture time of its own — otherwise
    the name is at best a confirmation of something already on screen, and at
    worst a contradiction that belongs to a different rule.
    """
    if context.capture.taken:
        return
    found = timestamp_from_filename(context.file.name)
    if found is None:
        return

    exact = found.precision == PRECISION_SECOND
    yield Finding(
        id="recovery.filename_time",
        category=Category.PLATFORM,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM if exact else Confidence.LOW,
        variant=found.precision,
        detail_variant=found.precision,
        params={
            "moment": found.exif_value if exact else found.moment.strftime("%Y-%m-%d"),
            "matched": found.matched,
            "source_keys": [found.source],
        },
        evidence={"filename": context.file.name, "precision": found.precision},
        # -o writes a new file. Restoring a date is a judgement call, and a
        # judgement call should not overwrite the only copy of the evidence.
        remediation=(
            f'exiftool -AllDates="{found.exif_value}" -o restored.jpg "{context.file.name}"'
        ),
    )


@rule("restart_interval_convention", Category.AUTHENTICITY, order=18)
def restart_interval_convention(context: Context) -> Iterable[Finding]:
    """One restart marker per row of blocks — a device convention.

    Restart markers let a decoder resynchronise after corruption, and a
    general-purpose library omits them entirely unless asked. Some camera
    pipelines emit exactly one per MCU row, which makes the interval a function
    of the image width rather than a setting.

    Cheap to check and it survives everything: the interval belongs to the
    compressed data, so a container rewrite carries it through untouched. It is
    corroboration for "a device compressed this", not a claim on its own, which
    is why it only speaks when the tables have already said the same thing.
    """
    print_ = context.jpeg
    if print_ is None or not print_.ok or context.has_camera_identity:
        return
    interval, width = print_.restart_interval, print_.width
    luma = print_.luma
    if not interval or not width or luma is None or not luma.repeats_first_row:
        return
    if not print_.sampling:
        return

    # One MCU is 8 pixels times the component's horizontal sampling factor.
    mcu_width = 8 * print_.sampling[0][0]
    expected = -(-width // mcu_width)  # ceiling division
    if interval != expected:
        return

    yield Finding(
        id="recovery.restart_per_row",
        category=Category.AUTHENTICITY,
        severity=Severity.INFO,
        confidence=Confidence.MEDIUM,
        params={"interval": interval},
        evidence={"restart_interval": interval, "mcus_per_row": expected, "width": width},
    )


@rule("nonsense_resolution", Category.PLATFORM, order=19)
def nonsense_resolution(context: Context) -> Iterable[Finding]:
    """A file claiming to be one dot per inch.

    XResolution and YResolution describe printing density, and every camera
    writes 72. A value of 1 is not a resolution anybody chose; it is what comes
    out when a writer copies a JFIF density field — where 1x1 means "no density,
    treat these as an aspect ratio" — into an Exif tag whose unit is inches.

    It has no consequence for the picture. It is worth reporting because it
    pins down that a specific rewriter handled the file, which is the sort of
    thing that lets two stripped photos be tied to the same source.
    """
    meta = context.meta
    if context.has_camera_identity:
        return
    x = meta.float("IFD0:XResolution", "XResolution")
    y = meta.float("IFD0:YResolution", "YResolution")
    if x != 1 or y != 1:
        return
    unit = (meta.str("IFD0:ResolutionUnit", "ResolutionUnit") or "").lower()
    if "inch" not in unit:
        return

    yield Finding(
        id="recovery.one_dpi",
        category=Category.PLATFORM,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        params={},
        evidence={"XResolution": x, "YResolution": y, "ResolutionUnit": unit},
    )


#: How a file says it is a screen capture rather than a photograph.
SCREENSHOT_MARKERS = ("screenshot", "screen shot", "screen capture", "знімок екрана")


@rule("never_had_it", Category.PLATFORM, order=20)
def never_had_it(context: Context) -> Iterable[Finding]:
    """Absent because it was removed, or absent because it never existed.

    A report that lists what is missing invites the reader to go looking for it.
    On a screenshot that search cannot succeed: nothing was photographed, so
    there was never a lens, a camera model or a location to record. Saying "no
    location" without saying why sends somebody hunting for a coordinate that
    has never existed anywhere.

    The distinction is the whole point of the tool. "Removed" is a fact about
    what somebody did to the file; "never existed" is a fact about what the file
    is.
    """
    if context.has_camera_identity or context.location.present:
        return
    marker = (context.meta.str("ExifIFD:UserComment", "UserComment") or "").strip().lower()
    from_tag = any(hint in marker for hint in SCREENSHOT_MARKERS)
    from_name = (match_filename(context.file.name) or "").endswith("screenshot")
    if not (from_tag or from_name):
        return

    yield Finding(
        id="recovery.screenshot",
        category=Category.PLATFORM,
        severity=Severity.INFO,
        confidence=Confidence.HIGH if from_tag else Confidence.MEDIUM,
        params={},
        evidence={"UserComment": marker or None, "filename": context.file.name},
    )
