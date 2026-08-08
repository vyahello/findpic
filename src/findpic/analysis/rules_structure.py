"""Rules that ask whether the file contains anything a photo should not.

This is triage, not analysis. findpic flags structure that looks wrong and tells
you what to look at next. It never executes, decodes, or "cleans" a payload, and
it does not pretend a metadata reader can decide whether something is malware.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from ..models import Category, Confidence, Finding, Severity
from ..util import truncate
from .context import Context
from .registry import rule

#: Trailing bytes below this are ordinary padding and alignment slack.
TRAILER_NOISE_FLOOR = 64
#: Above this, something substantial is riding along after the image ends.
TRAILER_ALARM = 1024

#: Metadata text long enough to be a container rather than a caption.
OVERSIZED_TEXT = 8 * 1024

#: Code-ish content in a metadata string. Kept narrow on purpose: these match
#: things that have no business in a caption, not merely unusual punctuation.
CODE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<\?php\b", re.I), "PHP open tag"),
    (re.compile(r"<script[\s>]", re.I), "HTML script tag"),
    (re.compile(r"\beval\s*\(", re.I), "eval() call"),
    (re.compile(r"\bbase64_decode\s*\(", re.I), "base64_decode() call"),
    (
        re.compile(r"\b(?:system|passthru|shell_exec|popen|proc_open)\s*\(", re.I),
        "shell execution call",
    ),
    (re.compile(r"\bsubprocess\.(?:run|Popen|call)\s*\(", re.I), "subprocess call"),
    (re.compile(r"<iframe[\s>]", re.I), "HTML iframe"),
    (re.compile(r"javascript\s*:", re.I), "javascript: URI"),
    (re.compile(r"\bpowershell(?:\.exe)?\b", re.I), "PowerShell invocation"),
    (re.compile(r"\bcmd\.exe\b", re.I), "Windows shell invocation"),
    (re.compile(r"/bin/(?:ba|z|da)?sh\b"), "Unix shell path"),
    (re.compile(r"\b(?:wget|curl)\s+https?://", re.I), "remote download command"),
    (re.compile(r"\bXMLHttpRequest\b|\bfetch\s*\(\s*['\"]https?://", re.I), "outbound HTTP call"),
    (re.compile(r"\bdocument\.(?:write|cookie)\b", re.I), "DOM manipulation"),
    (re.compile(r"^\s*(?:MZ|\x7fELF)"), "executable header"),
)

#: Tags whose values are legitimately long or base64 — exempt from the blob check.
BLOB_TOLERANT = re.compile(
    r"thumbnail|preview|image|profile|binary|data|matrix|curve|vector|opcode|"
    r"lut|blob|xmp:?toolkit|iccprofile|hdrgain",
    re.I,
)

#: A long unbroken base64-looking run, which is how payloads hide in text fields.
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{300,}={0,2}")

#: Bidirectional control characters used to disguise a filename's real extension.
BIDI_CONTROLS = {
    "‪",
    "‫",
    "‬",
    "‭",
    "‮",
    "⁦",
    "⁧",
    "⁨",
    "⁩",
    "‎",
    "‏",
}

#: Extensions that are executable or script content, not images.
DANGEROUS_EXTENSIONS = frozenset(
    {
        "exe",
        "scr",
        "com",
        "bat",
        "cmd",
        "pif",
        "msi",
        "ps1",
        "vbs",
        "js",
        "jar",
        "sh",
        "app",
        "dmg",
        "apk",
        "hta",
        "lnk",
        "reg",
        "dll",
        "iso",
    }
)

#: exiftool file types that are not images at all.
NON_IMAGE_TYPES = frozenset({"HTML", "XML", "TXT", "PDF", "ZIP", "RAR", "7Z", "EXE", "ELF", "SWF"})

#: Validation complaints that are spec pedantry rather than evidence of anything.
#: Real cameras trip these constantly — iPhones omit GPSVersionID, almost nobody
#: writes the xpacket wrapper — so treating them as suspicious would flag most of
#: the world's photographs.
BENIGN_WARNING = re.compile(
    r"^\[minor\]"
    r"|missing required"
    r"|out of sequence"
    r"|xpacket"
    r"|duplicate \w+ tag"
    r"|tag id \S+ .*(?:unknown|not standard)",
    re.I,
)


@rule("trailing_data", Category.STRUCTURAL, order=1)
def trailing_data(context: Context) -> Iterable[Finding]:
    """Bytes living past the point where the image structure ends."""
    scan = context.container
    if scan is None or scan.image_end is None:
        return
    extra = scan.trailing_bytes
    if extra <= TRAILER_NOISE_FLOOR:
        return

    substantial = extra >= TRAILER_ALARM
    yield Finding(
        id="structural.trailing_data",
        category=Category.STRUCTURAL,
        severity=Severity.WARNING if substantial else Severity.NOTICE,
        confidence=Confidence.HIGH,
        detail_variant="substantial" if substantial else "minor",
        params={
            "size_bytes": extra,
            "format": scan.format,
            "image_end": f"{scan.image_end:,}",
            "file_size": f"{scan.file_size:,}",
            "extra": extra,
            "filename": context.file.name,
        },
        evidence={
            "trailing_bytes": extra,
            "image_end": scan.image_end,
            "file_size": scan.file_size,
        },
        weight=45 if substantial else 8,
    )


@rule("truncated_image", Category.STRUCTURAL, order=1)
def truncated_image(context: Context) -> Iterable[Finding]:
    """The container structure never reaches a proper end marker."""
    scan = context.container
    if scan is None or not scan.truncated:
        return
    yield Finding(
        id="structural.truncated",
        category=Category.STRUCTURAL,
        severity=Severity.NOTICE,
        confidence=Confidence.HIGH,
        evidence={"format": scan.format, "file_size": scan.file_size},
        weight=15,
    )


@rule("multiple_images", Category.STRUCTURAL, order=2)
def multiple_images(context: Context) -> Iterable[Finding]:
    """More than one complete image in one file."""
    scan = context.container
    if scan is None or scan.image_count <= 1:
        return
    yield Finding(
        id="structural.multiple_images",
        category=Category.STRUCTURAL,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        count=scan.image_count,
        params={"filename": context.file.name},
        evidence={"image_count": scan.image_count},
    )


@rule("type_mismatch", Category.STRUCTURAL, order=2)
def type_mismatch(context: Context) -> Iterable[Finding]:
    """The real format disagreeing with the filename extension."""
    info = context.file
    actual = (info.file_type or "").upper()
    claimed = (info.extension or "").lower()
    if not actual or not claimed:
        return

    expected = (info.file_type_extension or "").lower()
    equivalent = {
        ("jpeg", "jpg"),
        ("jpg", "jpeg"),
        ("tiff", "tif"),
        ("tif", "tiff"),
        ("heif", "heic"),
        ("heic", "heif"),
    }
    if claimed == expected or (claimed, expected) in equivalent:
        return

    dangerous = actual in NON_IMAGE_TYPES
    yield Finding(
        id="structural.type_mismatch",
        category=Category.STRUCTURAL,
        severity=Severity.CRITICAL if dangerous else Severity.WARNING,
        confidence=Confidence.HIGH,
        detail_variant="dangerous" if dangerous else "renamed",
        params={"actual": actual, "claimed": claimed},
        evidence={"FileType": actual, "extension": claimed},
        weight=40 if dangerous else 12,
    )


@rule("filename_tricks", Category.STRUCTURAL, order=3)
def filename_tricks(context: Context) -> Iterable[Finding]:
    """Filenames engineered to display as something other than what they are."""
    name = context.file.name

    bidi = sorted(BIDI_CONTROLS & set(name))
    if bidi:
        codes = ", ".join(f"U+{ord(ch):04X}" for ch in bidi)
        yield Finding(
            id="structural.filename_bidi",
            category=Category.STRUCTURAL,
            severity=Severity.CRITICAL,
            confidence=Confidence.HIGH,
            params={"codes": codes},
            evidence={"filename": repr(name), "controls": codes},
            weight=70,
        )

    parts = name.lower().split(".")
    if len(parts) > 2 and parts[-1] in DANGEROUS_EXTENSIONS:
        yield Finding(
            id="structural.double_extension",
            category=Category.STRUCTURAL,
            severity=Severity.CRITICAL,
            confidence=Confidence.HIGH,
            params={"extension": parts[-1]},
            evidence={"filename": name},
            weight=70,
        )

    if any(unicodedata.category(ch) == "Cc" for ch in name):
        yield Finding(
            id="structural.filename_control_chars",
            category=Category.STRUCTURAL,
            severity=Severity.WARNING,
            confidence=Confidence.HIGH,
            evidence={"filename": repr(name)},
            weight=25,
        )


@rule("code_in_metadata", Category.STRUCTURAL, order=4)
def code_in_metadata(context: Context) -> Iterable[Finding]:
    """Executable-looking content sitting in a text metadata field."""
    hits: list[tuple[str, str, str]] = []
    for tag, value in context.meta.text_items():
        if BLOB_TOLERANT.search(tag):
            continue
        for pattern, description in CODE_PATTERNS:
            if pattern.search(value):
                hits.append((tag, description, truncate(value, 100)))
                break

    if not hits:
        return
    yield Finding(
        id="structural.code_in_metadata",
        category=Category.STRUCTURAL,
        severity=Severity.CRITICAL,
        confidence=Confidence.MEDIUM,
        count=len(hits),
        params={
            "hits": "; ".join(f"{tag} ({what})" for tag, what, _ in hits[:3]),
            "filename": context.file.name,
        },
        evidence={tag: sample for tag, _, sample in hits},
        weight=55,
    )


@rule("oversized_metadata", Category.STRUCTURAL, order=5)
def oversized_metadata(context: Context) -> Iterable[Finding]:
    """A metadata field far larger than any caption needs to be."""
    oversized: list[tuple[str, int]] = []
    blobs: list[str] = []
    for tag, value in context.meta.text_items():
        if len(value) >= OVERSIZED_TEXT:
            oversized.append((tag, len(value)))
        if not BLOB_TOLERANT.search(tag) and BASE64_BLOB.search(value):
            blobs.append(tag)

    for tag, size in oversized[:3]:
        yield Finding(
            id="structural.oversized_field",
            category=Category.STRUCTURAL,
            severity=Severity.NOTICE,
            confidence=Confidence.MEDIUM,
            params={"tag": tag, "size_bytes": size},
            evidence={"tag": tag, "length": size},
            weight=12,
        )

    if blobs:
        yield Finding(
            id="structural.encoded_blob",
            category=Category.STRUCTURAL,
            severity=Severity.WARNING,
            confidence=Confidence.LOW,
            count=len(blobs),
            params={"tags": ", ".join(blobs[:3])},
            evidence={"tags": blobs},
            weight=15,
        )


@rule("exiftool_complaints", Category.STRUCTURAL, order=6)
def exiftool_complaints(context: Context) -> Iterable[Finding]:
    """exiftool's own parse errors, which mean the structure is genuinely broken."""
    meta = context.meta
    if meta.errors:
        yield Finding(
            id="structural.parse_error",
            category=Category.STRUCTURAL,
            severity=Severity.WARNING,
            confidence=Confidence.HIGH,
            params={"details": "; ".join(truncate(e, 120) for e in meta.errors[:3])},
            evidence={"errors": meta.errors},
            weight=20,
        )

    serious = [w for w in meta.warnings if not BENIGN_WARNING.search(w)]
    if not serious:
        return
    yield Finding(
        id="structural.parse_warning",
        category=Category.STRUCTURAL,
        severity=Severity.NOTICE,
        confidence=Confidence.MEDIUM,
        count=len(serious),
        params={"details": "; ".join(truncate(w, 120) for w in serious[:3])},
        evidence={"warnings": serious},
        weight=10,
    )


@rule("pixel_bomb", Category.STRUCTURAL, order=7)
def pixel_bomb(context: Context) -> Iterable[Finding]:
    """Declared dimensions wildly out of proportion to the file's size."""
    image = context.image
    size = context.file.size_bytes
    if not (image.width and image.height and size):
        return
    pixels = image.width * image.height
    if pixels < 80_000_000:
        return
    bytes_per_pixel = size / pixels
    if bytes_per_pixel > 0.02:
        return
    yield Finding(
        id="structural.pixel_bomb",
        category=Category.STRUCTURAL,
        severity=Severity.WARNING,
        confidence=Confidence.MEDIUM,
        params={
            "megapixels": f"{pixels / 1_000_000:.0f}",
            "size_bytes": size,
            "gigabytes": f"{pixels * 3 / 1024**3:.1f}",
        },
        evidence={
            "width": image.width,
            "height": image.height,
            "file_bytes": size,
            "bytes_per_pixel": round(bytes_per_pixel, 5),
        },
        weight=40,
    )
