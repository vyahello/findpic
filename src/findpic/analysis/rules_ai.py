"""Rules about synthetic media and content provenance.

The honest position, stated here and repeated in the output: metadata can prove
a file *declares* itself AI-generated, but it can never prove a file is a real
photograph. Anything can be stripped. A silent file is not evidence of anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache

from ..models import Category, Confidence, Finding, Severity
from ..tables import AI_GENERATOR_SIGNATURES, DIGITAL_SOURCE_TYPES
from ..util import truncate
from .context import Context
from .registry import rule

#: Fields a generator writes its own name into. A value here was put there by
#: software describing itself, so a match is evidence about the file.
SIGNING_TAGS = (
    "IFD0:Software",
    "XMP-xmp:CreatorTool",
    "XMP-photoshop:Credit",
    "PNG:Software",
    "PNG:Parameters",
)

#: Free text a *person* wrote. A match here is evidence about the sentence, not
#: about the photograph: people write "gemini", "flux" and "imagen" in captions
#: for reasons that have nothing to do with how the picture was made.
CAPTION_TAGS = (
    "XMP-dc:Description",
    "XMP-dc:Creator",
    "ExifIFD:UserComment",
    "XMP-exif:UserComment",
    "IFD0:ImageDescription",
    "File:Comment",
    "PNG:Comment",
    "PNG:Description",
)


@cache
def _pattern(needle: str) -> re.Pattern[str]:
    """Word boundaries, not substrings.

    ``imagen`` inside the Spanish "la imagen fue tomada en Madrid" was accusing
    a real photograph of being AI-generated, and ``flux`` inside "magnetic flux
    experiment" did the same. The boundary is alphanumeric rather than ``\b``
    so that ``leonardo.ai`` and ``dall-e`` — needles that contain punctuation —
    still match at their own edges.
    """
    return re.compile(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])")


def _names(lowered: str, captions: bool) -> tuple[str, ...]:
    """Every generator named in one value, believed-anywhere needles first."""
    return tuple(
        name
        for needle, name, signing_only in AI_GENERATOR_SIGNATURES
        if not (captions and signing_only) and _pattern(needle).search(lowered)
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
    """A generator naming itself in a metadata field.

    Two findings, because there are two different claims. Software that signed
    a field is evidence about the file. A person who typed a tool's name into a
    caption is evidence about the caption, and saying so at WARNING — on a file
    the same report grades ORIGINAL — is the most damaging sentence this tool
    can print.
    """
    signed: dict[str, str] = {}
    mentioned: dict[str, str] = {}
    for tags, hits, captions in ((SIGNING_TAGS, signed, False), (CAPTION_TAGS, mentioned, True)):
        for tag in tags:
            value = context.meta.str(tag)
            if not value:
                continue
            evidence = f"{tag} = {truncate(value, 80)}"
            for name in _names(value.lower(), captions=captions):
                hits.setdefault(name, evidence)

    # A tool that signed the file and is also named in the caption is one fact.
    for name in signed:
        mentioned.pop(name, None)

    if signed:
        yield Finding(
            id="ai.generator_signature",
            category=Category.AI,
            severity=Severity.WARNING,
            confidence=Confidence.MEDIUM,
            params={"names": ", ".join(signed)},
            evidence=signed,
        )
    if mentioned:
        yield Finding(
            id="ai.generator_mentioned",
            category=Category.AI,
            severity=Severity.NOTICE,
            confidence=Confidence.LOW,
            params={"names": ", ".join(mentioned)},
            evidence=mentioned,
        )
