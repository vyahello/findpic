"""Fingerprint a JPEG from its compression structure, not its tags.

This module exists for the file that has nothing left to read. A picture that
went through a messenger arrives with its Exif rebuilt from scratch: no camera,
no timestamp, no coordinates. Every question findpic normally answers is
answered by tags that are gone.

But the *compression* survives, because it is the picture. A JPEG carries its
quantization tables, its Huffman tables, its restart interval and its segment
order in the clear, and those are chosen by whichever encoder wrote the file.
They are not metadata — they cannot be stripped without re-compressing the
image — so they keep speaking after everything else has been silenced.

What that supports and what it does not:

- It can say the tables are **not** the ones a general-purpose library uses.
  That is a fact, computed here from first principles: libjpeg and everything
  built on it scale the two example tables in Annex K of the JPEG standard by a
  quality setting, so if a table equals that scaling for some quality, the
  encoder was of that family and we can name the quality exactly.
- It can say two files were written by the same encoder at the same setting.
- It **cannot** recover a timestamp, a location, or a camera model. No amount of
  structural analysis puts back a number that was deleted. Everything here is
  attribution, never recovery, and the rules that consume it must say so.

Nothing decodes pixels. We follow length fields and read table bytes, which is
what keeps this safe to point at a hostile file.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

#: Zigzag order. Quantization tables are stored in it; humans read row-major.
ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
)  # fmt: skip

#: The two example quantization tables from Annex K of ITU-T T.81, row-major.
#: libjpeg ships these verbatim, and so does everything built on libjpeg —
#: Pillow, ImageMagick, GD, Go's image/jpeg, most server-side resizers. An
#: encoder that scales these is telling us it is a general-purpose library
#: rather than a camera.
ANNEX_K_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)  # fmt: skip

ANNEX_K_CHROMA = (
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
)  # fmt: skip

MARKER_NAMES = {
    0xC0: "SOF0",
    0xC1: "SOF1",
    0xC2: "SOF2",
    0xC3: "SOF3",
    0xC4: "DHT",
    0xC9: "SOF9",
    0xCA: "SOF10",
    0xCC: "DAC",
    0xD8: "SOI",
    0xD9: "EOI",
    0xDA: "SOS",
    0xDB: "DQT",
    0xDD: "DRI",
    0xFE: "COM",
}

#: Progressive and arithmetic-coded start-of-frame markers. A camera writing a
#: still to its own card does not produce these; a web pipeline often does.
PROGRESSIVE_SOF = frozenset({0xC2, 0xC6, 0xCA, 0xCE})
SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

#: Stop before a hostile file makes us walk forever. A real JPEG header is a few
#: hundred kilobytes at the very most, and we never need the scan data.
MAX_HEADER_BYTES = 8 * 1024 * 1024


def ijg_scale(table: tuple[int, ...], quality: int) -> tuple[int, ...]:
    """Scale an Annex K table the way libjpeg does, for one quality setting."""
    quality = max(1, min(100, quality))
    factor = 5000 // quality if quality < 50 else 200 - quality * 2
    return tuple(max(1, min(255, (value * factor + 50) // 100)) for value in table)


#: Every scaled Annex K table, precomputed once, keyed by its bytes. Membership
#: is then an exact dictionary lookup rather than a hundred comparisons.
_IJG_LUMA = {ijg_scale(ANNEX_K_LUMA, q): q for q in range(100, 0, -1)}
_IJG_CHROMA = {ijg_scale(ANNEX_K_CHROMA, q): q for q in range(100, 0, -1)}


@dataclass(frozen=True)
class QuantTable:
    """One quantization table, as the file stores it."""

    index: int
    #: Zigzag order, exactly as written.
    values: tuple[int, ...]
    precision: int = 8

    @property
    def natural(self) -> tuple[int, ...]:
        """Row-major, which is the order the numbers mean something in."""
        out = [0] * 64
        for position, value in enumerate(self.values):
            out[ZIGZAG[position]] = value
        return tuple(out)

    @property
    def dc_step(self) -> int:
        """The DC quantizer — the coarsest thing about the whole table."""
        return self.values[0]

    @property
    def total(self) -> int:
        return sum(self.values)

    @property
    def ijg_quality(self) -> int | None:
        """The libjpeg quality this table is, or None if it is not one.

        Exact match only. A near miss is not a quality setting, it is a
        different encoder, and reporting "about 92" for a camera table would
        invent a fact.
        """
        return _IJG_LUMA.get(self.natural) or _IJG_CHROMA.get(self.natural)

    @property
    def first_two_rows_identical(self) -> bool:
        """Whether rows 0 and 1 are the same — the bare structural fact."""
        rows = self.natural
        return rows[0:8] == rows[8:16]

    @property
    def repeats_first_row(self) -> bool:
        """Rows 0 and 1 identical, in the range where that means something.

        Annex K's first two rows have different shapes — 16,11,10,16… against
        12,12,14,19… — so scaling them can never make the rows agree. A table
        where they do agree was therefore not produced by the library, and the
        check needs no signature database to say so.

        Except at the ends of the scale, where the arithmetic collapses: at
        quality 100 every entry clamps to 1 and at quality 1 every entry clamps
        to 255, and a flat table's rows trivially match. Requiring three distinct
        values in the first row excludes exactly those cases — verified against
        all 100 library qualities for both tables, none of which pass.
        """
        rows = self.natural
        return rows[0:8] == rows[8:16] and len(set(rows[0:8])) >= 3


@dataclass(frozen=True)
class Segment:
    """One JPEG marker segment, located but not interpreted."""

    marker: int
    offset: int
    length: int
    #: The APPn payload's leading identifier, e.g. "JFIF", "Exif", "ICC_PROFILE".
    identifier: str = ""

    @property
    def name(self) -> str:
        if 0xE0 <= self.marker <= 0xEF:
            return f"APP{self.marker - 0xE0}"
        return MARKER_NAMES.get(self.marker, f"0x{self.marker:02X}")

    @property
    def label(self) -> str:
        return f"{self.name}/{self.identifier}" if self.identifier else self.name


@dataclass(frozen=True)
class JpegPrint:
    """What a JPEG's structure says about the encoder that wrote it."""

    segments: tuple[Segment, ...] = ()
    quant: tuple[QuantTable, ...] = ()
    width: int | None = None
    height: int | None = None
    progressive: bool = False
    restart_interval: int | None = None
    #: Per-component (horizontal, vertical) sampling factors from the SOF.
    sampling: tuple[tuple[int, int], ...] = ()
    dqt_segments: int = 0
    dht_segments: int = 0
    huffman_tables: int = 0
    truncated: bool = False
    error: str = ""
    _raw_tables: bytes = field(default=b"", repr=False)

    # ------------------------------------------------------------- derived

    @property
    def ok(self) -> bool:
        return bool(self.segments) and not self.error

    @property
    def luma(self) -> QuantTable | None:
        return self.quant[0] if self.quant else None

    @property
    def app_identifiers(self) -> tuple[str, ...]:
        return tuple(s.identifier for s in self.segments if s.identifier)

    @property
    def layout(self) -> str:
        """The segment order, as a comparable string.

        Encoders differ in where they put the tables relative to the frame
        header and in whether they emit one combined table segment or several.
        Two files with the same layout string went through the same writer.
        """
        return ",".join(s.label for s in self.segments)

    @property
    def table_signature(self) -> str:
        """A short hash of the quantization tables alone.

        Same signature means the same encoder at the same quality setting. It is
        deliberately independent of the Huffman tables and the segment order, so
        it survives a container rewrite — which is exactly the case where the
        main image and its own thumbnail stop agreeing.
        """
        if not self._raw_tables:
            return ""
        return hashlib.sha256(self._raw_tables).hexdigest()[:16]

    @property
    def ijg_quality(self) -> int | None:
        """The libjpeg quality setting, when the tables are the library's own.

        None means the tables are not scaled Annex K at all — which is the
        interesting answer, because it means a camera or a vendor pipeline chose
        them.
        """
        qualities = {table.ijg_quality for table in self.quant}
        if not qualities or None in qualities:
            return None
        return max(qualities)

    @property
    def uses_library_tables(self) -> bool:
        """True when every quantization table is a scaled Annex K table."""
        return bool(self.quant) and self.ijg_quality is not None

    @property
    def subsampling(self) -> str | None:
        """Chroma subsampling as "4:2:0" and friends."""
        if len(self.sampling) < 3:
            return "4:4:4" if len(self.sampling) == 1 else None
        horizontal, vertical = self.sampling[0]
        return {
            (1, 1): "4:4:4",
            (2, 1): "4:2:2",
            (1, 2): "4:4:0",
            (2, 2): "4:2:0",
        }.get((horizontal, vertical))

    @property
    def tables_before_frame(self) -> bool:
        """Whether the quantization tables precede the frame header.

        Most writers emit DQT then SOF. A writer that does the opposite is a
        different writer, and that alone has separated files here that agreed on
        everything else.
        """
        order = [s.marker for s in self.segments]
        if 0xDB not in order:
            return False
        frames = [i for i, m in enumerate(order) if m in SOF_MARKERS]
        return bool(frames) and order.index(0xDB) < frames[0]


def embedded_thumbnail(data: bytes) -> bytes | None:
    """The preview JPEG stored inside the Exif block, if there is one.

    Located by walking, not by trusting IFD1's ThumbnailOffset: that offset is
    relative to a TIFF header whose own position depends on the segment, and a
    file that has been rewritten by three different tools is exactly the file
    where an off-by-six lands you in the middle of the picture. The thumbnail is
    the only complete JPEG inside the Exif segment, so finding its SOI and
    walking to the matching EOI needs no arithmetic that can be wrong.

    Worth having because the thumbnail is written once, early, and then tends to
    be carried along untouched. It can therefore disagree with the image around
    it — a different encoder, or a different shape — and each disagreement says
    something about what happened to the file after the picture was taken.
    """
    for segment in _parse(data).segments:
        if segment.marker != 0xE1 or segment.identifier != "Exif":
            continue
        body = data[segment.offset + 4 : segment.offset + 2 + segment.length]
        start = body.find(b"\xff\xd8\xff")
        if start < 0:
            return None
        end = body.rfind(b"\xff\xd9")
        if end <= start:
            return None
        return bytes(body[start : end + 2])
    return None


def _parse(data: bytes) -> JpegPrint:
    """Walk marker segments until the scan starts. Never touches scan data."""
    if not data.startswith(b"\xff\xd8"):
        return JpegPrint(error="not a JPEG")

    segments: list[Segment] = []
    quant: list[QuantTable] = []
    raw_tables = bytearray()
    width = height = None
    progressive = False
    restart = None
    sampling: list[tuple[int, int]] = []
    dqt_count = dht_count = huffman_count = 0

    offset = 2
    limit = min(len(data), MAX_HEADER_BYTES)
    while offset < limit - 1:
        if data[offset] != 0xFF:
            return JpegPrint(
                tuple(segments), tuple(quant), width, height, progressive, restart,
                tuple(sampling), dqt_count, dht_count, huffman_count,
                truncated=True, error="lost marker alignment",
            )  # fmt: skip
        marker = data[offset + 1]
        if marker == 0xD9:  # EOI before any scan: an empty or broken file
            break
        if offset + 4 > limit:
            break
        length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if length < 2 or offset + 2 + length > limit:
            return JpegPrint(
                tuple(segments), tuple(quant), width, height, progressive, restart,
                tuple(sampling), dqt_count, dht_count, huffman_count,
                truncated=True, error="segment length runs past the file",
            )  # fmt: skip

        body = data[offset + 4 : offset + 2 + length]
        identifier = ""
        if 0xE0 <= marker <= 0xEF:
            # APPn payloads start with a NUL-terminated identifier. Some are
            # URIs (XMP), so keep it short rather than printing the namespace.
            identifier = body.split(b"\x00", 1)[0][:16].decode("latin-1", "replace").strip()

        segments.append(Segment(marker, offset, length, identifier))

        if marker == 0xDB:
            dqt_count += 1
            raw_tables += body
            position = 0
            while position < len(body):
                precision = 8 if body[position] >> 4 == 0 else 16
                index = body[position] & 0x0F
                position += 1
                count = 64 if precision == 8 else 128
                chunk = body[position : position + count]
                if len(chunk) < count:
                    break
                values = (
                    tuple(chunk) if precision == 8 else tuple(struct.unpack(">64H", bytes(chunk)))
                )
                quant.append(QuantTable(index, values, precision))
                position += count
        elif marker == 0xC4:
            dht_count += 1
            position = 0
            while position + 17 <= len(body):
                counts = body[position + 1 : position + 17]
                position += 17 + sum(counts)
                huffman_count += 1
        elif marker == 0xDD and len(body) >= 2:
            restart = struct.unpack(">H", body[:2])[0]
        elif marker in SOF_MARKERS and len(body) >= 6:
            progressive = marker in PROGRESSIVE_SOF
            height, width = struct.unpack(">HH", body[1:5])
            components = body[5]
            for index in range(components):
                base = 6 + index * 3
                if base + 1 < len(body):
                    sampling.append((body[base + 1] >> 4, body[base + 1] & 0x0F))
        elif marker == 0xDA:
            break

        offset += 2 + length

    quant.sort(key=lambda table: table.index)
    return JpegPrint(
        segments=tuple(segments),
        quant=tuple(quant),
        width=width,
        height=height,
        progressive=progressive,
        restart_interval=restart,
        sampling=tuple(sampling),
        dqt_segments=dqt_count,
        dht_segments=dht_count,
        huffman_tables=huffman_count,
        _raw_tables=bytes(raw_tables),
    )


def fingerprint_bytes(data: bytes) -> JpegPrint:
    """Fingerprint a JPEG already in memory — an embedded thumbnail, usually."""
    try:
        return _parse(data)
    except (struct.error, IndexError) as error:  # malformed input is expected
        return JpegPrint(error=f"{type(error).__name__}: {error}")


def fingerprint(path: str | Path) -> JpegPrint:
    """Fingerprint a JPEG on disk, reading only as much as the header needs."""
    try:
        with open(path, "rb") as handle:
            return fingerprint_bytes(handle.read(MAX_HEADER_BYTES))
    except OSError as error:
        return JpegPrint(error=str(error))
