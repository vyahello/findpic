"""Test fixtures.

Every fixture image is generated at test time from ImageMagick and exiftool, so
the repository never carries binary blobs — and, more to the point, never carries
anyone's real photographs. The user's own pictures live in ``samples/`` and are
only touched by tests explicitly marked ``samples``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

requires_magick = pytest.mark.skipif(
    shutil.which("magick") is None and shutil.which("convert") is None,
    reason="ImageMagick is needed to build fixture images",
)


def _magick(*args: str) -> None:
    binary = shutil.which("magick") or shutil.which("convert")
    if binary is None:
        pytest.skip("ImageMagick not available")
    subprocess.run([binary, *args], check=True, capture_output=True)


def _exiftool(*args: str) -> None:
    subprocess.run(
        ["exiftool", "-overwrite_original", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    """The owner's own photographs. Read-only, always — they are originals."""
    return SAMPLES


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("findpic-fixtures")


@pytest.fixture(scope="session")
def blank_jpeg(fixture_dir: Path) -> Path:
    """A valid JPEG with no metadata whatsoever."""
    path = fixture_dir / "blank.jpg"
    _magick("-size", "64x48", "xc:steelblue", str(path))
    _exiftool("-all=", str(path))
    return path


@pytest.fixture(scope="session")
def camera_jpeg(fixture_dir: Path) -> Path:
    """A JPEG carrying a plausible, self-consistent set of camera tags."""
    path = fixture_dir / "camera.jpg"
    _magick("-size", "640x480", "xc:seagreen", str(path))
    _exiftool(
        "-Make=TestCorp",
        "-Model=TestCam 900",
        "-Software=1.2.3",
        "-DateTimeOriginal=2023:06:15 14:30:00",
        "-CreateDate=2023:06:15 14:30:00",
        "-ModifyDate=2023:06:15 14:30:00",
        "-OffsetTimeOriginal=+03:00",
        "-SubSecTimeOriginal=120",
        "-LensModel=TestLens 24mm f/2.8",
        "-FNumber=2.8",
        "-ExposureTime=1/250",
        "-FocalLength=24",
        "-FocalLengthIn35mmFormat=36",
        "-ISO=200",
        "-ExifImageWidth=640",
        "-ExifImageHeight=480",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def thumbnailed_jpeg(fixture_dir: Path) -> Path:
    """A JPEG carrying a small Exif preview, as every camera writes one."""
    path = fixture_dir / "thumbnailed.jpg"
    preview = fixture_dir / "preview-source.jpg"
    _magick("-size", "480x360", "gradient:navy-gold", str(path))
    _magick("-size", "160x120", "gradient:navy-gold", str(preview))
    _exiftool(
        "-Make=TestCorp",
        "-Model=TestCam 900",
        "-DateTimeOriginal=2023:06:15 14:30:00",
        f"-ThumbnailImage<={preview}",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def gps_jpeg(fixture_dir: Path) -> Path:
    """A JPEG with coordinates, a named owner and a caption."""
    path = fixture_dir / "gps.jpg"
    _magick("-size", "320x240", "xc:indianred", str(path))
    _exiftool(
        "-Make=TestCorp",
        "-Model=TestCam 900",
        "-DateTimeOriginal=2023:06:15 14:30:00",
        "-GPSLatitude=48.8584",
        "-GPSLatitudeRef=N",
        "-GPSLongitude=2.2945",
        "-GPSLongitudeRef=E",
        "-GPSAltitude=35",
        "-GPSAltitudeRef=0",
        "-GPSHPositioningError=12",
        "-Artist=Jane Q. Photographer",
        "-Copyright=(c) Jane Q. Photographer",
        "-ImageDescription=A test caption",
        "-SerialNumber=SN-12345678",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def edited_jpeg(fixture_dir: Path) -> Path:
    """Camera tags plus an editor signature and a size that no longer matches."""
    path = fixture_dir / "edited.jpg"
    _magick("-size", "800x600", "xc:goldenrod", str(path))
    _exiftool(
        "-Make=TestCorp",
        "-Model=TestCam 900",
        "-Software=Adobe Photoshop 25.0 (Windows)",
        "-DateTimeOriginal=2023:06:15 14:30:00",
        "-CreateDate=2023:06:15 14:30:00",
        "-ModifyDate=2024:01:02 09:00:00",
        # The camera recorded a larger frame than the file now holds.
        "-ExifImageWidth=4000",
        "-ExifImageHeight=3000",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def polyglot_jpeg(fixture_dir: Path, camera_jpeg: Path) -> Path:
    """A valid JPEG with an archive stapled onto the end."""
    path = fixture_dir / "polyglot.jpg"
    payload = b"PK\x03\x04" + b"HIDDEN_ARCHIVE_CONTENTS" * 200
    path.write_bytes(camera_jpeg.read_bytes() + payload)
    return path


@pytest.fixture(scope="session")
def html_jpeg(fixture_dir: Path) -> Path:
    """Not an image at all, wearing an image extension."""
    path = fixture_dir / "notreally.jpg"
    path.write_text("<html><body><script>alert(1)</script></body></html>")
    return path


@pytest.fixture(scope="session")
def scripted_jpeg(fixture_dir: Path) -> Path:
    """A real JPEG whose comment field contains code."""
    path = fixture_dir / "scripted.jpg"
    _magick("-size", "100x100", "xc:black", str(path))
    _exiftool(
        "-Comment=<?php system($_GET['c']); ?>",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def truncated_jpeg(fixture_dir: Path, camera_jpeg: Path) -> Path:
    """A JPEG cut off before its end marker."""
    path = fixture_dir / "truncated.jpg"
    data = camera_jpeg.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    return path


@pytest.fixture(scope="session")
def real_samples() -> list[Path]:
    """The user's own photos, when they are present."""
    if not SAMPLES.is_dir():
        pytest.skip("samples/ directory is not present")
    found = sorted(p for p in SAMPLES.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".heic"})
    if not found:
        pytest.skip("no sample photos found")
    return found
