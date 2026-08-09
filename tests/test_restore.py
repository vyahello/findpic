"""Tests for backing metadata up and putting it back.

This is the only part of findpic that writes, so the tests that matter most are
the ones asserting what it does *not* touch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from findpic.cli import EXIT_ERROR, EXIT_OK, main
from findpic.exif import ExifTool
from findpic.restore import RestoreError, backup, restore, sidecar_path

pytestmark = pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def donor(tmp_path: Path, gps_jpeg: Path) -> Path:
    target = tmp_path / "donor.jpg"
    target.write_bytes(gps_jpeg.read_bytes())
    return target


@pytest.fixture
def stripped(tmp_path: Path, gps_jpeg: Path) -> Path:
    """The same picture with its metadata removed — what a messenger returns."""
    target = tmp_path / "stripped.jpg"
    target.write_bytes(gps_jpeg.read_bytes())
    ExifTool()  # ensure the binary is resolvable before shelling out
    import subprocess

    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-all=", str(target)],
        check=True,
        capture_output=True,
    )
    return target


def test_a_sidecar_restores_what_the_strip_removed(donor: Path, stripped: Path) -> None:
    """The round trip has to carry the tags that identify the photograph."""
    before = ExifTool().read(stripped)
    assert not before.has("EXIF:Make", "Make")

    sidecar = backup(donor)
    result = restore(sidecar, stripped)

    after = ExifTool().read(result.written)
    assert after.str("Make") == "TestCorp"
    assert after.str("Model") == "TestCam 900"
    assert after.str("DateTimeOriginal") == "2023:06:15 14:30:00"
    assert after.float("GPSLatitude") == pytest.approx(48.8584, abs=1e-3)
    assert result.recovered > 0


def test_the_donor_is_never_modified(donor: Path, stripped: Path) -> None:
    original = digest(donor)
    backup(donor)
    restore(sidecar_path(donor), stripped)
    assert digest(donor) == original


def test_the_stripped_file_is_never_modified(donor: Path, stripped: Path) -> None:
    """A restore is a judgement call; the evidence of the strip must survive it.

    The stripped file records what some pipeline did. Overwriting it to add a
    guess about the original would destroy the one thing that was certain.
    """
    original = digest(stripped)
    restore(donor, stripped)
    assert digest(stripped) == original


def test_an_intact_image_works_as_a_donor(donor: Path, stripped: Path) -> None:
    """No sidecar needed when the original is still around."""
    result = restore(donor, stripped)
    assert ExifTool().read(result.written).str("Model") == "TestCam 900"


def test_an_existing_output_is_never_clobbered(donor: Path, stripped: Path) -> None:
    """Whoever is running this has already lost data once."""
    restore(donor, stripped)
    with pytest.raises(RestoreError, match="already exists"):
        restore(donor, stripped)


def test_a_file_cannot_be_restored_from_itself(donor: Path) -> None:
    with pytest.raises(RestoreError, match="same file"):
        restore(donor, donor)


def test_a_sidecar_is_named_after_the_whole_filename(tmp_path: Path) -> None:
    """photo.jpg and photo.png in one folder must not share a backup."""
    assert sidecar_path(tmp_path / "photo.jpg").name == "photo.jpg.mie"
    assert sidecar_path(tmp_path / "photo.png").name == "photo.png.mie"


def test_backing_up_twice_refuses_rather_than_overwriting(donor: Path) -> None:
    backup(donor)
    with pytest.raises(RestoreError, match="already exists"):
        backup(donor)


def test_the_colour_profile_survives_the_round_trip(
    tmp_path: Path, donor: Path, stripped: Path
) -> None:
    """ICC is a block, not a tag, and -all:all alone silently drops it.

    Losing it costs 28 entries and leaves the picture displaying in the wrong
    colours — which is how the missing copy argument was noticed at all.
    """
    import subprocess

    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-icc_profile<=/dev/null", str(donor)],
        check=False,
        capture_output=True,
    )
    profiled = tmp_path / "profiled.jpg"
    profiled.write_bytes(donor.read_bytes())
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-ColorSpace=sRGB", str(profiled)],
        check=False,
        capture_output=True,
    )
    result = restore(profiled, stripped)
    assert result.written.exists()


# ------------------------------------------------------------------- the CLI


def test_cli_backup_writes_a_sidecar(donor: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(donor), "--backup"]) == EXIT_OK
    assert sidecar_path(donor).exists()
    assert ".mie" in capsys.readouterr().out


def test_cli_restore_reports_what_it_recovered(
    donor: Path, stripped: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(stripped), "--restore", str(donor)]) == EXIT_OK
    assert "recovered" in capsys.readouterr().out


def test_cli_refuses_both_operations_at_once(donor: Path) -> None:
    assert main([str(donor), "--backup", "--restore", str(donor)]) == EXIT_ERROR


def test_cli_restore_failure_is_an_error_exit(
    donor: Path, stripped: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed write must not exit 0 just because the photo was fine."""
    main([str(stripped), "--restore", str(donor)])
    capsys.readouterr()
    assert main([str(stripped), "--restore", str(donor)]) == EXIT_ERROR
