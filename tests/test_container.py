"""Tests for the container walker.

This module is the one place where a wrong answer has real consequences: it
decides whether findpic says "clean file" or "something is hidden in here". The
naive implementation (searching backwards for FFD9) passes the easy cases and
fails exactly when it matters, so the payload tests below are the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from findpic.container import scan


def test_clean_jpeg_ends_where_the_file_ends(camera_jpeg: Path) -> None:
    result = scan(camera_jpeg)
    assert result.format == "JPEG"
    assert result.image_end == result.file_size
    assert result.trailing_bytes == 0
    assert result.image_count == 1
    assert not result.truncated


def test_appended_payload_is_measured_exactly(camera_jpeg: Path, tmp_path: Path) -> None:
    payload = b"PK\x03\x04" + bytes(range(256)) * 4
    target = tmp_path / "poly.jpg"
    target.write_bytes(camera_jpeg.read_bytes() + payload)

    result = scan(target)
    assert result.trailing_bytes == len(payload)


def test_payload_containing_the_end_marker_is_not_hidden(camera_jpeg: Path, tmp_path: Path) -> None:
    """The regression that motivated walking the structure.

    A payload containing FFD9 fools a reverse byte search into reporting only the
    bytes after the *last* occurrence, concealing the rest.
    """
    payload = b"A" * 500 + b"\xff\xd9" + b"B" * 50
    target = tmp_path / "sneaky.jpg"
    target.write_bytes(camera_jpeg.read_bytes() + payload)

    result = scan(target)
    assert result.trailing_bytes == len(payload), "payload with an embedded EOI hid itself"


def test_multiple_images_are_counted_not_flagged(
    camera_jpeg: Path, gps_jpeg: Path, tmp_path: Path
) -> None:
    target = tmp_path / "multi.jpg"
    target.write_bytes(camera_jpeg.read_bytes() + gps_jpeg.read_bytes())

    result = scan(target)
    assert result.image_count == 2
    assert result.trailing_bytes == 0


def test_payload_after_a_second_image_is_still_found(
    camera_jpeg: Path, gps_jpeg: Path, tmp_path: Path
) -> None:
    payload = b"X" * 300
    target = tmp_path / "multi_plus.jpg"
    target.write_bytes(camera_jpeg.read_bytes() + gps_jpeg.read_bytes() + payload)

    result = scan(target)
    assert result.image_count == 2
    assert result.trailing_bytes == len(payload)


def test_truncated_file_is_reported(truncated_jpeg: Path) -> None:
    result = scan(truncated_jpeg)
    assert result.truncated is True


def test_non_image_yields_no_format(html_jpeg: Path) -> None:
    result = scan(html_jpeg)
    assert result.format is None
    assert result.trailing_bytes == 0


def test_missing_file_does_not_raise(tmp_path: Path) -> None:
    result = scan(tmp_path / "nope.jpg")
    assert result.format is None
    assert result.file_size == 0


def test_png_is_walked_to_iend(tmp_path: Path) -> None:
    pytest.importorskip("subprocess")
    import shutil
    import subprocess

    binary = shutil.which("magick") or shutil.which("convert")
    if binary is None:
        pytest.skip("ImageMagick not available")
    target = tmp_path / "image.png"
    subprocess.run(
        [binary, "-size", "32x32", "xc:navy", str(target)], check=True, capture_output=True
    )

    result = scan(target)
    assert result.format == "PNG"
    assert result.trailing_bytes == 0

    appended = tmp_path / "image_plus.png"
    appended.write_bytes(target.read_bytes() + b"Z" * 128)
    assert scan(appended).trailing_bytes == 128
