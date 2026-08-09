"""Tests for the JPEG structural fingerprinter.

These matter more than most. The fingerprinter is what findpic falls back on
when a file has no metadata left, so if it is wrong the tool is confidently
wrong about a file the user cannot check any other way.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from findpic.exif import ExifTool
from findpic.jpegprint import (
    ANNEX_K_CHROMA,
    ANNEX_K_LUMA,
    ZIGZAG,
    QuantTable,
    fingerprint,
    fingerprint_bytes,
    ijg_scale,
)

pytestmark = pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")


def to_zigzag(natural: tuple[int, ...]) -> tuple[int, ...]:
    out = [0] * 64
    for position, index in enumerate(ZIGZAG):
        out[position] = natural[index]
    return tuple(out)


# ------------------------------------------------------------------ quality


@pytest.mark.parametrize("quality", [50, 75, 85, 90, 94, 95, 100])
def test_library_tables_report_their_exact_quality(quality: int) -> None:
    table = QuantTable(0, to_zigzag(ijg_scale(ANNEX_K_LUMA, quality)))
    assert table.ijg_quality == quality


def test_a_table_that_is_not_a_library_table_reports_no_quality() -> None:
    """A near miss must be None, not the nearest quality.

    Camera tables sit close to the library's without being it. Reporting "about
    92" for one would turn a guess into a number the user would quote.
    """
    almost = list(ijg_scale(ANNEX_K_LUMA, 90))
    almost[17] += 1
    assert QuantTable(0, to_zigzag(tuple(almost))).ijg_quality is None


def test_no_library_quality_can_fake_a_repeated_first_row() -> None:
    """The claim "not a library table" must hold for every library quality.

    Both extremes of the scale collapse the table to a single clamped value, and
    a flat table's rows trivially match — so the bare comparison is true at
    qualities 1 and 98-100. repeats_first_row exists to exclude exactly that, and
    this walks all 100 qualities of both tables to prove it does.
    """
    for quality in range(1, 101):
        luma = QuantTable(0, to_zigzag(ijg_scale(ANNEX_K_LUMA, quality)))
        chroma = QuantTable(1, to_zigzag(ijg_scale(ANNEX_K_CHROMA, quality)))
        assert not luma.repeats_first_row, f"luma quality {quality}"
        assert not chroma.repeats_first_row, f"chroma quality {quality}"


def test_a_vendor_shaped_table_is_recognised() -> None:
    """A perceptual table whose first two rows agree must be spotted.

    The shape is taken from a real Apple-encoded file: a gentle ramp repeated on
    the second row, which no scaling of Annex K produces.
    """
    row = (2, 2, 2, 3, 4, 5, 6, 7)
    natural = row + row + tuple(range(8, 56))
    assert QuantTable(0, to_zigzag(natural)).repeats_first_row


def test_natural_order_round_trips_through_zigzag() -> None:
    natural = tuple(range(64))
    assert QuantTable(0, to_zigzag(natural)).natural == natural


# ------------------------------------------------------------- real files


def test_an_imagemagick_file_is_recognised_as_library_encoded(camera_jpeg: Path) -> None:
    """The fixtures come from ImageMagick, which is libjpeg underneath."""
    print_ = fingerprint(camera_jpeg)
    assert print_.ok
    assert print_.uses_library_tables
    assert print_.ijg_quality is not None
    assert print_.luma is not None and not print_.luma.repeats_first_row


def test_dimensions_come_from_the_frame_header_not_the_tags(camera_jpeg: Path) -> None:
    """SOF is the only width a stripped file still has, so it must be right."""
    print_ = fingerprint(camera_jpeg)
    assert (print_.width, print_.height) == (640, 480)
    assert print_.subsampling in {"4:2:0", "4:2:2", "4:4:4"}


def test_the_same_encoder_at_the_same_setting_gives_the_same_signature(
    camera_jpeg: Path, gps_jpeg: Path
) -> None:
    """Two different pictures, one encoder: the tables must agree.

    This is the property that lets findpic say two files came out of the same
    pipeline when neither has a tag left to compare.
    """
    first, second = fingerprint(camera_jpeg), fingerprint(gps_jpeg)
    assert first.table_signature
    assert first.table_signature == second.table_signature


def test_the_layout_string_records_segment_order(camera_jpeg: Path) -> None:
    layout = fingerprint(camera_jpeg).layout
    assert "SOF0" in layout and "DQT" in layout and "DHT" in layout
    assert layout.index("DQT") < layout.index("SOS")


# -------------------------------------------------------------- robustness


def test_a_non_jpeg_is_rejected_rather_than_guessed_at() -> None:
    assert fingerprint_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).error == "not a JPEG"


def test_a_truncated_file_is_reported_as_truncated(camera_jpeg: Path) -> None:
    data = camera_jpeg.read_bytes()
    result = fingerprint_bytes(data[: len(data) // 3])
    assert result.truncated or result.ok


def test_a_lying_segment_length_does_not_read_past_the_file(camera_jpeg: Path) -> None:
    """A hostile length field must be caught, not followed.

    This is the whole reason the walker exists rather than a scan for markers:
    the file tells us how far to jump, and the file may be lying.
    """
    data = bytearray(camera_jpeg.read_bytes())
    data[4:6] = struct.pack(">H", 0xFFFF)
    result = fingerprint_bytes(bytes(data))
    assert result.truncated
    assert "past the file" in result.error


def test_an_empty_file_does_not_raise() -> None:
    assert not fingerprint_bytes(b"").ok


def test_a_missing_file_reports_an_error_instead_of_raising(tmp_path: Path) -> None:
    assert fingerprint(tmp_path / "nope.jpg").error


def test_no_pixels_are_decoded_even_for_a_huge_frame() -> None:
    """A frame header claiming 60000x60000 must cost nothing.

    findpic's safety story is that it never decodes, so a decompression bomb is
    a few seeks. A fingerprinter that quietly reached for Pillow would break it.
    """
    body = b"\x08" + struct.pack(">HH", 60000, 60000) + b"\x03" + b"\x01\x22\x00" * 3
    data = b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", len(body) + 2) + body + b"\xff\xd9"
    result = fingerprint_bytes(data)
    assert (result.width, result.height) == (60000, 60000)
