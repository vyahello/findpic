"""Tests for the exiftool wrapper, especially its hardening."""

from __future__ import annotations

from pathlib import Path

import pytest

from findpic.exif import (
    ExifTool,
    ExifToolTimeout,
    Metadata,
    UnreadableFile,
    hash_file,
)

pytestmark = pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")


@pytest.fixture(scope="module")
def exiftool() -> ExifTool:
    return ExifTool()


def test_version_is_reported(exiftool: ExifTool) -> None:
    assert exiftool.version()


def test_reads_both_passes(exiftool: ExifTool, gps_jpeg: Path) -> None:
    meta = exiftool.read(gps_jpeg)
    # Human pass renders coordinates as degrees/minutes/seconds…
    assert "deg" in str(meta.get("GPS:GPSLatitude"))
    # …while the numeric pass gives a float we can compute with.
    assert meta.float("GPS:GPSLatitude") == pytest.approx(48.8584, abs=1e-3)


def test_lookup_is_flexible(exiftool: ExifTool, camera_jpeg: Path) -> None:
    meta = exiftool.read(camera_jpeg)
    assert meta.str("IFD0:Model") == "TestCam 900"
    assert meta.str("Model") == "TestCam 900", "bare tag name should resolve"
    assert meta.str("ifd0:model") == "TestCam 900", "lookup should be case-insensitive"
    assert meta.str("EXIF:Model") == "TestCam 900", "a wrong group should fall back"
    assert meta.str("NoSuchTag") is None
    assert meta.str("NoSuchTag", default="fallback") == "fallback"


def test_first_present_value_wins(exiftool: ExifTool, camera_jpeg: Path) -> None:
    meta = exiftool.read(camera_jpeg)
    assert meta.str("NoSuchTag", "IFD0:Model") == "TestCam 900"


def test_group_helpers(exiftool: ExifTool, camera_jpeg: Path) -> None:
    meta = exiftool.read(camera_jpeg)
    assert meta.has_group("IFD0")
    assert not meta.has_group("Nikon")
    assert meta.group("IFD0")
    assert meta.tag_count > 10
    assert sum(meta.group_counts().values()) == meta.tag_count


def test_binary_placeholders_are_recognised(exiftool: ExifTool, real_samples) -> None:
    meta = exiftool.read(real_samples[0])
    if not meta.has("ThumbnailImage"):
        pytest.skip("sample has no embedded thumbnail")
    assert meta.is_binary("ThumbnailImage")
    # Binary blobs must not leak into the text scan.
    assert all(not value.startswith("(Binary data") for _, value in meta.text_items())


def test_missing_file_raises(exiftool: ExifTool, tmp_path: Path) -> None:
    with pytest.raises(UnreadableFile):
        exiftool.read(tmp_path / "does_not_exist.jpg")


def test_directory_raises(exiftool: ExifTool, tmp_path: Path) -> None:
    with pytest.raises(UnreadableFile):
        exiftool.read(tmp_path)


def test_empty_file_raises(exiftool: ExifTool, tmp_path: Path) -> None:
    empty = tmp_path / "empty.jpg"
    empty.touch()
    with pytest.raises(UnreadableFile):
        exiftool.read(empty)


def test_size_ceiling_is_enforced(camera_jpeg: Path) -> None:
    tiny_limit = ExifTool(max_bytes=10)
    with pytest.raises(UnreadableFile, match="ceiling"):
        tiny_limit.read(camera_jpeg)


def test_filename_starting_with_dash_is_not_read_as_a_flag(
    exiftool: ExifTool, camera_jpeg: Path, tmp_path: Path
) -> None:
    """Resolving to an absolute path is what makes this safe."""
    hostile = tmp_path / "-ver"
    hostile.write_bytes(camera_jpeg.read_bytes())
    meta = exiftool.read(hostile)
    assert meta.str("IFD0:Model") == "TestCam 900"


def test_filename_with_spaces_and_quotes(
    exiftool: ExifTool, camera_jpeg: Path, tmp_path: Path
) -> None:
    hostile = tmp_path / "a photo 'with' \"quotes\" and $(id).jpg"
    hostile.write_bytes(camera_jpeg.read_bytes())
    meta = exiftool.read(hostile)
    assert meta.str("IFD0:Model") == "TestCam 900"


def test_shell_metacharacters_are_not_executed(
    exiftool: ExifTool,
    camera_jpeg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filename that would be a command injection under shell=True."""
    monkeypatch.chdir(tmp_path)
    hostile = tmp_path / "x; touch pwned; echo .jpg"
    hostile.write_bytes(camera_jpeg.read_bytes())

    meta = exiftool.read(hostile)

    assert not (tmp_path / "pwned").exists(), "filename was interpreted by a shell"
    assert meta.str("IFD0:Model") == "TestCam 900"


def test_non_image_is_read_without_raising(exiftool: ExifTool, html_jpeg: Path) -> None:
    meta = exiftool.read(html_jpeg)
    assert meta.str("File:FileType") == "HTML"


def test_timeout_is_enforced(camera_jpeg: Path) -> None:
    impatient = ExifTool(timeout=0)
    with pytest.raises(ExifToolTimeout):
        impatient.read(camera_jpeg)


def test_warnings_are_collected(exiftool: ExifTool, truncated_jpeg: Path) -> None:
    meta = exiftool.read(truncated_jpeg)
    assert meta.warnings or meta.errors


def test_extract_binary_rejects_odd_tag_names(exiftool: ExifTool, camera_jpeg: Path) -> None:
    with pytest.raises(ValueError):
        exiftool.extract_binary(camera_jpeg, "Thumbnail; rm -rf /")


def test_metadata_indexes_numeric_only_tags() -> None:
    """`-struct` and `-n` produce different key sets; both must be reachable."""
    meta = Metadata(
        path="x.jpg",
        human={"XMP:RegionInfo": {"RegionList": []}},
        numeric={"XMP:RegionAreaW": 0.5},
    )
    assert meta.float("RegionAreaW") == 0.5
    assert meta.tag_count == 2


def test_hash_file_matches_hashlib(camera_jpeg: Path) -> None:
    import hashlib

    data = camera_jpeg.read_bytes()
    sha, md5 = hash_file(camera_jpeg)
    assert sha == hashlib.sha256(data).hexdigest()
    assert md5 == hashlib.md5(data).hexdigest()
