"""Find where an image file's structure actually ends.

Searching backwards for a JPEG's ``FFD9`` end marker is the obvious approach and
it is wrong: that byte pair occurs inside entropy-coded scan data and, more to
the point, inside whatever someone appended to the file. A payload containing
``FFD9`` would hide most of itself from a naive scan.

So we walk the container properly — JPEG segment by segment, PNG chunk by chunk —
and report the offset where the image legitimately finishes. Everything past that
is extra.

Nothing here decodes pixels. We only follow length fields, so a decompression
bomb costs us a few seeks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

CHUNK = 64 * 1024

#: JPEG markers that stand alone with no length field following them.
_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})

JPEG_SOI = b"\xff\xd8"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ContainerScan:
    """Where the image ends and what, if anything, follows it."""

    format: str | None
    image_end: int | None
    file_size: int
    #: Number of complete images found. JPEGs with a second embedded image
    #: (Apple HDR, Samsung portrait) legitimately carry more than one.
    image_count: int = 1
    truncated: bool = False

    @property
    def trailing_bytes(self) -> int:
        if self.image_end is None:
            return 0
        return max(0, self.file_size - self.image_end)


def _skip_entropy(handle: BinaryIO) -> int | None:
    """Advance past entropy-coded scan data.

    Returns the offset of the next real marker's ``FF``, leaving the file
    positioned there. Stuffed ``FF00`` bytes and restart markers are part of the
    scan, not the end of it.
    """
    previous_ff = False
    ff_offset = -1
    while True:
        base = handle.tell()
        chunk = handle.read(CHUNK)
        if not chunk:
            return None
        for index, byte in enumerate(chunk):
            if previous_ff:
                if byte == 0x00 or 0xD0 <= byte <= 0xD7:
                    previous_ff = False
                    continue
                if byte == 0xFF:
                    ff_offset = base + index
                    continue
                handle.seek(ff_offset)
                return ff_offset
            if byte == 0xFF:
                previous_ff = True
                ff_offset = base + index


def _scan_one_jpeg(handle: BinaryIO) -> int | None:
    """Walk one JPEG's markers. Returns the offset just past its EOI."""
    depth = 0
    while True:
        byte = handle.read(1)
        if not byte:
            return None
        if byte != b"\xff":
            # Stray padding between segments; resync on the next FF.
            continue
        # Fill bytes: any run of FFs before the marker is legal.
        marker_byte = handle.read(1)
        while marker_byte == b"\xff":
            marker_byte = handle.read(1)
        if not marker_byte:
            return None

        marker = marker_byte[0]

        if marker == 0xD8:  # nested SOI — an embedded thumbnail inside a segment
            depth += 1
            continue
        if marker == 0xD9:  # EOI
            if depth:
                depth -= 1
                continue
            return handle.tell()
        if marker in _STANDALONE or marker == 0x00:
            continue

        length_bytes = handle.read(2)
        if len(length_bytes) < 2:
            return None
        length = int.from_bytes(length_bytes, "big")
        if length < 2:
            return None

        handle.seek(length - 2, 1)
        # Start of scan: entropy-coded data follows the segment header.
        if marker == 0xDA and _skip_entropy(handle) is None:
            return None


def scan_jpeg(handle: BinaryIO, file_size: int) -> ContainerScan:
    """Walk JPEG markers to the end of the last legitimate image.

    Multi-picture files (Apple and Samsung portrait modes, some HDR pipelines)
    append a second complete JPEG after the first one's EOI. That is a normal
    container, not a payload, so we keep walking while the next bytes are another
    SOI and count the images instead of calling the second one "trailing data".
    """
    handle.seek(0)
    if handle.read(2) != JPEG_SOI:
        return ContainerScan(format=None, image_end=None, file_size=file_size)

    images = 0
    end: int | None = None
    while True:
        found = _scan_one_jpeg(handle)
        if found is None:
            return ContainerScan("JPEG", end, file_size, images, truncated=True)
        images += 1
        end = found
        if end >= file_size:
            break
        handle.seek(end)
        if handle.read(2) != JPEG_SOI:
            break
        handle.seek(end + 2)

    return ContainerScan("JPEG", end, file_size, images)


def scan_png(handle: BinaryIO, file_size: int) -> ContainerScan:
    """Walk PNG chunks to IEND."""
    handle.seek(len(PNG_SIGNATURE))
    while True:
        header = handle.read(8)
        if len(header) < 8:
            return ContainerScan("PNG", None, file_size, truncated=True)
        length = int.from_bytes(header[:4], "big")
        chunk_type = header[4:8]
        if length > file_size:
            return ContainerScan("PNG", None, file_size, truncated=True)
        # Chunk data plus its 4-byte CRC.
        handle.seek(length + 4, 1)
        if chunk_type == b"IEND":
            return ContainerScan("PNG", handle.tell(), file_size)


def scan(path: str | Path) -> ContainerScan:
    """Locate the structural end of an image file."""
    target = Path(path)
    try:
        file_size = target.stat().st_size
        with open(target, "rb") as handle:
            signature = handle.read(8)
            if signature.startswith(JPEG_SOI):
                return scan_jpeg(handle, file_size)
            if signature == PNG_SIGNATURE:
                return scan_png(handle, file_size)
    except OSError:
        return ContainerScan(format=None, image_end=None, file_size=0)
    return ContainerScan(format=None, image_end=None, file_size=file_size)
