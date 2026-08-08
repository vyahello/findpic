"""Rules about synthetic media and content provenance.

The honest position, stated here and repeated in the output: metadata can prove
a file *declares* itself AI-generated, but it can never prove a file is a real
photograph. Anything can be stripped. A silent file is not evidence of anything.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import Category, Confidence, Finding, Severity
from ..tables import AI_GENERATOR_SIGNATURES, DIGITAL_SOURCE_TYPES
from ..util import truncate
from .context import Context
from .registry import rule

#: Fields a generator is likely to sign its name in.
PROVENANCE_TAGS = (
    "IFD0:Software",
    "XMP-xmp:CreatorTool",
    "XMP-dc:Creator",
    "XMP-dc:Description",
    "XMP-photoshop:Credit",
    "ExifIFD:UserComment",
    "IFD0:ImageDescription",
    "File:Comment",
    "PNG:Software",
    "PNG:Parameters",
    "PNG:Comment",
    "PNG:Description",
    "XMP-exif:UserComment",
)


@rule("declared_source_type", Category.AI, order=1)
def declared_source_type(context: Context) -> Iterable[Finding]:
    """IPTC's DigitalSourceType — the one field designed to answer this question."""
    value = context.meta.str("XMP-iptcExt:DigitalSourceType", "DigitalSourceType")
    if not value:
        return
    token = value.rstrip("/").rsplit("/", 1)[-1].lower()
    is_ai = DIGITAL_SOURCE_TYPES.get(token)

    if is_ai is None:
        yield Finding(
            id="ai.source_type_unknown",
            category=Category.AI,
            severity=Severity.NOTICE,
            confidence=Confidence.MEDIUM,
            params={"value": truncate(value, 60)},
            evidence={"DigitalSourceType": value},
        )
        return

    yield Finding(
        id="ai.declared_source_type",
        category=Category.AI,
        severity=Severity.WARNING if is_ai else Severity.INFO,
        confidence=Confidence.HIGH,
        params={"source_type_keys": [token]},
        evidence={"DigitalSourceType": value},
    )


@rule("generator_signature", Category.AI, order=2)
def generator_signature(context: Context) -> Iterable[Finding]:
    """A generator naming itself in a metadata field."""
    hits: dict[str, str] = {}
    for tag in PROVENANCE_TAGS:
        value = context.meta.str(tag)
        if not value:
            continue
        lowered = value.lower()
        for needle, name in AI_GENERATOR_SIGNATURES:
            if needle in lowered:
                hits.setdefault(name, f"{tag} = {truncate(value, 80)}")
                break

    if not hits:
        return
    yield Finding(
        id="ai.generator_signature",
        category=Category.AI,
        severity=Severity.WARNING,
        confidence=Confidence.MEDIUM,
        params={"names": ", ".join(hits)},
        evidence=hits,
    )


@rule("diffusion_parameters", Category.AI, order=3)
def diffusion_parameters(context: Context) -> Iterable[Finding]:
    """The prompt/seed block that Stable Diffusion front-ends write into PNGs."""
    # A1111 writes to Parameters; ComfyUI to Workflow/Prompt; converters and
    # "save as JPEG" paths frequently dump the same block into a comment.
    candidates = (
        "PNG:Parameters",
        "Parameters",
        "PNG:Prompt",
        "PNG:Workflow",
        "PNG:Comment",
        "File:Comment",
        "ExifIFD:UserComment",
        "IFD0:ImageDescription",
    )
    for tag in candidates:
        value = context.meta.str(tag)
        if not value:
            continue
        markers = sum(
            marker in value.lower()
            for marker in (
                "steps:",
                "sampler:",
                "cfg scale",
                "seed:",
                "model hash",
                "negative prompt",
                "denoising strength",
            )
        )
        # One keyword could be coincidence in a caption; three is a parameter block.
        if not (markers >= 3 or tag.endswith(("Parameters", "Workflow", "Prompt"))):
            continue
        yield Finding(
            id="ai.diffusion_parameters",
            category=Category.AI,
            severity=Severity.WARNING,
            confidence=Confidence.HIGH,
            params={"sample": truncate(value, 160)},
            evidence={tag: truncate(value, 500)},
        )
        return


@rule("content_credentials", Category.AI, order=4)
def content_credentials(context: Context) -> Iterable[Finding]:
    """A C2PA manifest — present, but not verified by us."""
    meta = context.meta
    if not (meta.has_group("JUMBF") or meta.has("JUMBF:JUMDLabel", "C2PA")):
        return
    yield Finding(
        id="ai.c2pa_manifest",
        category=Category.AI,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        params={"filename": context.file.name},
        evidence={"groups": sorted(g for g in meta.group_names() if "JUMBF" in g.upper())},
    )


@rule("no_provenance_context", Category.AI, order=9)
def no_provenance_context(context: Context) -> Iterable[Finding]:
    """State the limit explicitly rather than letting silence imply 'genuine'."""
    if context.device.has_makernotes or context.has_camera_identity:
        return
    if context.meta.tag_count > 40:
        return
    yield Finding(
        id="ai.cannot_determine",
        category=Category.AI,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        evidence={"tag_count": context.meta.tag_count},
    )
