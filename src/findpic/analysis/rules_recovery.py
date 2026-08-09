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
    if not main.uses_library_tables:
        return
    preview_luma = preview.luma
    if preview_luma is None or not preview_luma.repeats_first_row:
        return

    yield Finding(
        id="recovery.preview_older",
        category=Category.AUTHENTICITY,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        params={"quality": main.ijg_quality or "?"},
        evidence={
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
