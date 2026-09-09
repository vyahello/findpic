"""Tests for backing metadata up and putting it back.

This is the only part of findpic that writes, so the tests that matter most are
the ones asserting what it does *not* touch.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from findpic.cli import EXIT_ERROR, EXIT_OK, main
from findpic.exif import ExifTool
from findpic.restore import RestoreError, backup, clean, restore, sidecar_path

pytestmark = pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resized(destination: Path, source: Path) -> Path:
    """A copy of ``source`` at half its width — a different picture, to findpic."""
    binary = shutil.which("magick") or shutil.which("convert")
    if binary is None:
        pytest.skip("ImageMagick is needed to build a differently-sized copy")
    subprocess.run(
        [binary, str(source), "-resize", "50%", str(destination)],
        check=True,
        capture_output=True,
    )
    return destination


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


# ------------------------------------------------------- refusing to forge


def test_a_donor_of_a_different_size_is_refused(tmp_path: Path, donor: Path) -> None:
    """The failure this whole safeguard exists for.

    Pointed at two unrelated photographs, restore() will happily produce a file
    claiming a camera, a moment and a place that were never true of it — and
    findpic then read that file back and graded it LIKELY ORIGINAL, printing the
    fabricated coordinates as the location. Differing dimensions are the cheap
    half of "these are not the same picture", and they catch it.
    """
    other = resized(tmp_path / "different.jpg", donor)
    with pytest.raises(RestoreError, match="different sizes"):
        restore(donor, other)


def test_force_allows_a_deliberate_size_mismatch(tmp_path: Path, donor: Path) -> None:
    """Restoring onto a resized copy is legitimate — it just has to be meant."""
    smaller = resized(tmp_path / "smaller.jpg", donor)
    assert restore(donor, smaller, force=True).written.exists()


def test_every_restored_file_says_it_was_restored(donor: Path, stripped: Path) -> None:
    """Unmarked, a restored file is indistinguishable from an original.

    That is the whole risk: the values assert a camera and a place with exactly
    the confidence of a photograph that recorded them itself, and nothing
    downstream can tell. The marker is what makes it visible.
    """
    result = restore(donor, stripped)
    meta = ExifTool().read(result.written)
    assert "findpic" in (meta.str("HistorySoftwareAgent") or "")
    assert "metadata_restored" in (meta.str("HistoryAction") or "")
    assert donor.name in (meta.str("HistoryParameters") or "")


def test_findpic_reports_a_restored_file_as_restored(donor: Path, stripped: Path) -> None:
    """The marker is worthless if the analysis walks past it."""
    from findpic.analysis import analyze
    from findpic.analysis.context import AnalysisOptions

    result = restore(donor, stripped)
    report = analyze(result.written, options=AnalysisOptions(geocode=False))
    assert "recovery.restored" in {f.id for f in report.findings}


def test_the_donor_marker_cannot_be_overwritten_by_the_donor(
    tmp_path: Path, donor: Path, stripped: Path
) -> None:
    """The donor carries its own XMP history in the real world.

    The marker is written after the copy for exactly this reason; if the order
    ever flips, a donor with a history block would erase the evidence.
    """
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-q",
            "-XMP-xmpMM:HistoryAction=edited",
            "-XMP-xmpMM:HistorySoftwareAgent=SomeEditor 9.0",
            str(donor),
        ],
        check=False,
        capture_output=True,
    )
    result = restore(donor, stripped)
    assert "findpic" in (ExifTool().read(result.written).str("HistorySoftwareAgent") or "")


# ------------------------------------------------------------------- --clean


def test_clean_writes_beside_the_original_keeping_the_suffix(tmp_path: Path, gps_png: Path) -> None:
    """A PNG must not come back as a JPEG.

    exiftool refuses to change a file's type on the way out, so every command
    that hard-coded `.jpg` died on anything else — and a HEIC that returned as a
    JPEG would not be the same picture anyway.
    """
    source = tmp_path / "shot.png"
    shutil.copy(gps_png, source)
    result = clean(source)
    assert result.written == tmp_path / "shot.clean.png"
    assert result.written.exists()
    assert result.removed > 0


def test_clean_never_touches_the_input(tmp_path: Path, gps_jpeg: Path) -> None:
    source = tmp_path / "photo.jpg"
    shutil.copy(gps_jpeg, source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    clean(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_clean_refuses_to_clobber_an_existing_target(tmp_path: Path, gps_jpeg: Path) -> None:
    source = tmp_path / "photo.jpg"
    shutil.copy(gps_jpeg, source)
    clean(source)
    with pytest.raises(RestoreError, match="already exists"):
        clean(source)


def test_clean_keeps_orientation_and_icc(tmp_path: Path, camera_jpeg: Path) -> None:
    """The two things that are not metadata in any sense a reader cares about:
    which way up the picture goes, and what its colours mean."""
    source = tmp_path / "photo.jpg"
    shutil.copy(camera_jpeg, source)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-Orientation=6", "-n", str(source)],
        check=True,
        capture_output=True,
    )
    result = clean(source)
    kept = subprocess.run(
        ["exiftool", "-s3", "-Orientation", "-n", str(result.written)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert kept.stdout.strip() == "6"


def test_clean_refuses_to_report_success_when_the_camera_survives(tmp_path: Path, magick) -> None:
    """exiftool cannot delete IFD0 from a TIFF, and findpic accepts .tif and the
    whole TIFF-based raw family. It prints "[minor] Can't delete IFD0", exits 0,
    and leaves Make and Model exactly where they were."""
    source = tmp_path / "raw.tif"
    magick("-size", "64x48", "xc:steelblue", str(source))
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-Make=TestCorp", "-Model=TestCam", str(source)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(RestoreError, match="survived"):
        clean(source)


def test_clean_and_backup_together_are_refused(
    tmp_path: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "photo.jpg"
    shutil.copy(gps_jpeg, source)
    assert main([str(source), "--clean", "--backup"]) == EXIT_ERROR
    assert not (tmp_path / "photo.clean.jpg").exists()


def test_clean_out_into_a_directory_keeps_each_name(
    tmp_path: Path, gps_jpeg: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one, two = tmp_path / "one.jpg", tmp_path / "two.jpg"
    shutil.copy(gps_jpeg, one)
    shutil.copy(camera_jpeg, two)
    out = tmp_path / "stripped"
    out.mkdir()
    assert main([str(one), str(two), "--clean", "--out", str(out)]) == EXIT_OK
    capsys.readouterr()
    assert (out / "one.clean.jpg").exists()
    assert (out / "two.clean.jpg").exists()


def test_clean_out_as_one_file_is_refused_for_many_inputs(
    tmp_path: Path, gps_jpeg: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two inputs and one output name means the second either clobbers the first
    or dies; neither is something to discover after the fact."""
    one, two = tmp_path / "one.jpg", tmp_path / "two.jpg"
    shutil.copy(gps_jpeg, one)
    shutil.copy(camera_jpeg, two)
    assert main([str(one), str(two), "--clean", "--out", str(tmp_path / "only.jpg")]) == EXIT_ERROR
    assert not (tmp_path / "only.jpg").exists()


def test_the_restore_marker_is_what_makes_a_restored_file_detectable(
    tmp_path: Path, gps_jpeg: Path
) -> None:
    """Restoring writes a marker, and removing the marker removes the evidence.

    That is a structural limit, not a bug findpic can close: the marker lives in
    the XMP packet and anyone may delete the packet. What findpic must never do
    is *print the command that deletes it* — which it did, under
    privacy.named_people, as `-xmp:all=`. That half is a bug, and
    test_the_named_people_fix_leaves_the_restore_marker covers it.
    """
    from findpic.analysis import AnalysisOptions, analyze

    source = tmp_path / "photo.jpg"
    shutil.copy(gps_jpeg, source)
    sidecar = backup(source)
    stripped = tmp_path / "stripped.jpg"
    shutil.copy(gps_jpeg, stripped)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-all=", str(stripped)],
        check=True,
        capture_output=True,
    )
    written = restore(sidecar, stripped).written

    marked = {f.id for f in analyze(written, options=AnalysisOptions(geocode=False)).findings}
    assert "recovery.restored" in marked
    assert "authenticity.xmp_history" in marked

    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-xmp:all=", str(written)],
        check=True,
        capture_output=True,
    )
    after = {f.id for f in analyze(written, options=AnalysisOptions(geocode=False)).findings}
    assert "recovery.restored" not in after
    assert "authenticity.xmp_history" not in after
