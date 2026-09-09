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
def magick():
    """Build an image, skipping the test when ImageMagick is not installed.

    A fixture rather than an import: `from tests.conftest import _magick` works
    from the repository root and nowhere else, because `tests` is not a package
    and only pytest's rootdir insertion makes it look like one. It fails on CI.
    """
    return _magick


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
def identity_jpeg(fixture_dir: Path) -> Path:
    """Every tag IDENTITY_TAGS scans, including the email and the phone number.

    Written where exiftool will actually take each one — the group a value ends
    up in is the whole point of this fixture, because findpic finds tags by bare
    name across every group and its fixes did not.
    """
    path = fixture_dir / "identity.jpg"
    _magick("-size", "64x48", "xc:slategray", str(path))
    _exiftool(
        "-Artist=Jane Q. Photographer",
        "-Copyright=(c) Jane Q. Photographer",
        "-OwnerName=Jane Q. Photographer",
        "-XMP-dc:Rights=All rights reserved",
        "-IPTC:By-line=Jane Q. Photographer",
        "-IPTC:By-lineTitle=Staff",
        "-IPTC:Credit=The Daily Example",
        "-IPTC:Source=Example Wire",
        "-IPTC:Contact=jane@example.com",
        "-XMP-iptcCore:CreatorWorkEmail=jane@example.com",
        "-XMP-iptcCore:CreatorWorkTelephone=+44 20 7946 0000",
        "-XMP-iptcCore:CreatorWorkURL=https://example.com",
        "-XMP-photoshop:AuthorsPosition=Chief Photographer",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def text_jpeg(fixture_dir: Path) -> Path:
    """Every free-text field, including the six the old fix left behind."""
    path = fixture_dir / "text.jpg"
    _magick("-size", "64x48", "xc:tan", str(path))
    _exiftool(
        "-UserComment=a user comment",
        "-ImageDescription=an image description",
        "-XMP-dc:Description=an XMP description",
        "-XMP-dc:Title=an XMP title",
        "-IPTC:Caption-Abstract=an IPTC caption",
        "-IPTC:Headline=an IPTC headline",
        "-IPTC:ObjectName=an IPTC object name",
        "-IPTC:SpecialInstructions=do not publish before Friday",
        "-Comment=a JPEG comment",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def ids_jpeg(fixture_dir: Path) -> Path:
    """Device identifiers, including the xmpMM trio the old fix could not reach.

    No MakerNotes: exiftool will not create a vendor block from scratch, so the
    Apple identifiers can only be exercised against a real photograph.
    """
    path = fixture_dir / "ids.jpg"
    _magick("-size", "64x48", "xc:plum", str(path))
    _exiftool(
        "-SerialNumber=SN-12345678",
        "-LensSerialNumber=LSN-87654321",
        "-ImageUniqueID=0123456789abcdef0123456789abcdef",
        "-XMP-xmpMM:DocumentID=xmp.did:11111111-1111-1111-1111-111111111111",
        "-XMP-xmpMM:InstanceID=xmp.iid:22222222-2222-2222-2222-222222222222",
        "-XMP-xmpMM:OriginalDocumentID=xmp.did:33333333-3333-3333-3333-333333333333",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def named_people_jpeg(fixture_dir: Path) -> Path:
    """A human name attached to the picture, in the two places findpic reads."""
    path = fixture_dir / "people.jpg"
    _magick("-size", "64x48", "xc:khaki", str(path))
    _exiftool(
        "-XMP-mwg-rs:RegionType=Face",
        "-XMP-mwg-rs:RegionName=Alice Example",
        "-XMP-mwg-rs:RegionAreaX=0.5",
        "-XMP-mwg-rs:RegionAreaY=0.5",
        "-XMP-mwg-rs:RegionAreaW=0.2",
        "-XMP-mwg-rs:RegionAreaH=0.2",
        "-XMP-iptcExt:PersonInImage=Bob Example",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def shadowed_jpeg(fixture_dir: Path) -> Path:
    """Identity and caption data in groups the scan list does not name.

    findpic asks for ``IPTC:Source`` and Metadata answers with
    ``XMP-dc:Source``, because a group-qualified miss falls back to a bare-name
    match. Photoshop and Lightroom write these routinely, and every per-group
    fix ran as a no-op on them — exit 0, "1 image files copied", finding intact.
    """
    path = fixture_dir / "shadowed.jpg"
    _magick("-size", "64x48", "xc:cadetblue", str(path))
    _exiftool(
        "-XMP-dc:Source=a dc source",
        "-XMP-photoshop:Credit=a photoshop credit",
        "-XMP-xmp:Description=an xmp description",
        "-XMP-iptcExt:Headline=an iptcExt headline",
        str(path),
    )
    return path


@pytest.fixture(scope="session")
def gps_png(fixture_dir: Path) -> Path:
    """Coordinates in a container that is not a JPEG.

    PNG rather than HEIC on purpose. Every printed fix hard-coded ``.jpg`` as
    its output and died with "Can't create JPEG files from other types" on
    anything else — and PNG reproduces that error exactly while needing nothing
    beyond the ImageMagick every other fixture here already requires. A HEIC
    fixture would need libheif plus an x265 plugin, which CI does not have.
    """
    path = fixture_dir / "gps.png"
    _magick("-size", "64x48", "xc:seagreen", str(path))
    _exiftool(
        "-GPSLatitude=48.8584",
        "-GPSLatitudeRef=N",
        "-GPSLongitude=2.2945",
        "-GPSLongitudeRef=E",
        str(path),
    )
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
