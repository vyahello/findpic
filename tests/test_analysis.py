"""Tests for the analysis engine, its rules, and the verdict model."""

from __future__ import annotations

from pathlib import Path

import pytest

from findpic.analysis import AnalysisOptions, analyze
from findpic.analysis.registry import all_rules
from findpic.exif import ExifTool
from findpic.models import Category, Confidence, Report, Severity, VerdictLevel

pytestmark = pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")

#: Never let a test reach the network or waste time hashing fixture images.
OFFLINE = AnalysisOptions(geocode=False)


def run(path: Path, options: AnalysisOptions = OFFLINE) -> Report:
    return analyze(path, options=options)


def finding_ids(report: Report) -> set[str]:
    return {f.id for f in report.findings}


# ------------------------------------------------------------------ extraction


def test_camera_fields_are_extracted(camera_jpeg: Path) -> None:
    report = run(camera_jpeg)
    assert report.device.make == "TestCorp"
    assert report.device.model == "TestCam 900"
    assert report.capture.taken.startswith("2023-06-15 14:30:00")
    assert report.capture.taken_offset == "+03:00"
    assert report.capture.iso == 200
    assert report.file.sha256


def test_gps_is_extracted_and_rendered(gps_jpeg: Path) -> None:
    report = run(gps_jpeg)
    location = report.location
    assert location.present
    assert location.latitude == pytest.approx(48.8584, abs=1e-3)
    assert location.longitude == pytest.approx(2.2945, abs=1e-3)
    assert location.dms.endswith("E")
    assert location.accuracy_m == 12
    assert "openstreetmap.org" in location.osm_url
    assert location.geo_uri.startswith("geo:")


def test_geocoding_stays_off_when_disabled(gps_jpeg: Path) -> None:
    report = run(gps_jpeg, AnalysisOptions(geocode=False))
    assert report.location.place is None
    assert report.location.geocode_error is None


def test_hashing_can_be_skipped(camera_jpeg: Path) -> None:
    report = run(camera_jpeg, AnalysisOptions(geocode=False, hash_file=False))
    assert report.file.sha256 is None


# ----------------------------------------------------------------- originality


def test_clean_camera_file_shows_no_editing_signals(camera_jpeg: Path) -> None:
    """The fixture is hand-tagged ImageMagick output, not a camera file.

    It legitimately trips the encoder-fingerprint and missing-MakerNotes rules,
    so the useful assertion is that the *editing* signals stay silent. Only a
    genuine camera file earns ORIGINAL — see the real-sample test below.
    """
    report = run(camera_jpeg)
    ids = finding_ids(report)
    assert "authenticity.modify_date_differs" not in ids
    assert "authenticity.editor_software" not in ids
    assert "authenticity.dimension_mismatch" not in ids
    assert "authenticity.xmp_history" not in ids


def test_libjpeg_encoder_is_recognised(camera_jpeg: Path) -> None:
    """ImageMagick encodes through libjpeg, which exiftool can fingerprint.

    A camera that claims to be a camera but writes libjpeg's quantization tables
    was re-encoded — a signal that survives even a pristine-looking Exif block.
    """
    report = run(camera_jpeg)
    assert "authenticity.jpeg_digest" in finding_ids(report)
    finding = next(f for f in report.findings if f.id == "authenticity.jpeg_digest")
    assert finding.variant == "library"
    assert finding.confidence is Confidence.LOW


def test_editor_software_is_detected(edited_jpeg: Path) -> None:
    report = run(edited_jpeg)
    assert "authenticity.editor_software" in finding_ids(report)
    assert report.device.editor == "Adobe Photoshop"
    assert report.verdicts["originality"].level.rank >= VerdictLevel.POOR.rank


def test_resize_is_detected(edited_jpeg: Path) -> None:
    report = run(edited_jpeg)
    assert "authenticity.dimension_mismatch" in finding_ids(report)


def test_resave_timestamp_is_detected(edited_jpeg: Path) -> None:
    report = run(edited_jpeg)
    assert "authenticity.modify_date_differs" in finding_ids(report)


def test_stripped_file_is_unknown_not_suspicious(blank_jpeg: Path) -> None:
    """The most important honesty property in the whole tool."""
    report = run(blank_jpeg)
    originality = report.verdicts["originality"]
    assert originality.level is VerdictLevel.UNKNOWN
    assert originality.label(report.translator) == "UNKNOWN"
    assert report.verdicts["privacy"].level is VerdictLevel.GOOD
    assert report.verdicts["structure"].level is VerdictLevel.GOOD


def test_bare_version_software_is_not_an_editor(camera_jpeg: Path) -> None:
    """`Software: 1.2.3` is a firmware version, not an application."""
    report = run(camera_jpeg)
    assert report.device.editor is None
    assert "authenticity.editor_software" not in finding_ids(report)


# --------------------------------------------------------------------- privacy


def test_gps_drives_privacy_exposure(gps_jpeg: Path) -> None:
    report = run(gps_jpeg)
    ids = finding_ids(report)
    assert "privacy.gps_location" in ids
    assert report.verdicts["privacy"].level.rank >= VerdictLevel.POOR.rank

    gps = next(f for f in report.findings if f.id == "privacy.gps_location")
    assert gps.severity is Severity.CRITICAL
    assert gps.remediation and "-gps:all=" in gps.remediation


def test_identity_and_serial_are_reported(gps_jpeg: Path) -> None:
    ids = finding_ids(run(gps_jpeg))
    assert "privacy.identity_tags" in ids
    assert "privacy.device_identifiers" in ids


def test_caption_text_is_reported(gps_jpeg: Path) -> None:
    assert "privacy.free_text" in finding_ids(run(gps_jpeg))


def test_no_metadata_means_no_privacy_findings(blank_jpeg: Path) -> None:
    report = run(blank_jpeg)
    assert not report.by_category(Category.PRIVACY)


def test_every_privacy_remediation_writes_to_a_copy(gps_jpeg: Path) -> None:
    """findpic must never hand the user a command that eats their original."""
    for finding in run(gps_jpeg).findings:
        if not finding.remediation:
            continue
        assert "-overwrite_original" not in finding.remediation
        assert "-o " in finding.remediation


# ------------------------------------------------------------------ structural


def test_appended_payload_is_flagged(polyglot_jpeg: Path) -> None:
    report = run(polyglot_jpeg)
    assert "structural.trailing_data" in finding_ids(report)
    assert report.verdicts["structure"].level.rank >= VerdictLevel.POOR.rank


def test_wrong_format_for_extension_is_critical(html_jpeg: Path) -> None:
    report = run(html_jpeg)
    finding = next(f for f in report.findings if f.id == "structural.type_mismatch")
    assert finding.severity is Severity.CRITICAL
    assert report.verdicts["structure"].level.rank >= VerdictLevel.POOR.rank


def test_code_in_metadata_is_flagged(scripted_jpeg: Path) -> None:
    report = run(scripted_jpeg)
    assert "structural.code_in_metadata" in finding_ids(report)


def test_clean_file_has_no_structural_findings(camera_jpeg: Path) -> None:
    report = run(camera_jpeg)
    assert not report.by_category(Category.STRUCTURAL)
    assert report.verdicts["structure"].level is VerdictLevel.GOOD


def test_bidi_filename_is_critical(camera_jpeg: Path, tmp_path: Path) -> None:
    hostile = tmp_path / "holiday‮gpj.exe"
    hostile.write_bytes(camera_jpeg.read_bytes())
    report = run(hostile)
    ids = finding_ids(report)
    assert "structural.filename_bidi" in ids
    assert report.verdicts["structure"].level is VerdictLevel.BAD


def test_double_extension_is_critical(camera_jpeg: Path, tmp_path: Path) -> None:
    hostile = tmp_path / "photo.jpg.exe"
    hostile.write_bytes(camera_jpeg.read_bytes())
    assert "structural.double_extension" in finding_ids(run(hostile))


# ----------------------------------------------------------------- the machine


def test_axes_are_independent(gps_jpeg: Path) -> None:
    """Privacy exposure is scored without reference to the other two axes."""
    report = run(gps_jpeg)
    assert report.verdicts["privacy"].level.rank >= VerdictLevel.POOR.rank
    assert report.verdicts["structure"].level is VerdictLevel.GOOD
    # Privacy findings must not leak into the originality score.
    assert all(f.category is not Category.PRIVACY for f in report.verdicts["originality"].reasons)


def test_no_rule_crashes_on_any_fixture(
    blank_jpeg: Path,
    camera_jpeg: Path,
    gps_jpeg: Path,
    edited_jpeg: Path,
    polyglot_jpeg: Path,
    html_jpeg: Path,
    scripted_jpeg: Path,
    truncated_jpeg: Path,
) -> None:
    fixtures = (
        blank_jpeg,
        camera_jpeg,
        gps_jpeg,
        edited_jpeg,
        polyglot_jpeg,
        html_jpeg,
        scripted_jpeg,
        truncated_jpeg,
    )
    for path in fixtures:
        report = run(path)
        failed = [f.id for f in report.findings if f.id.startswith("internal.")]
        assert not failed, f"{path.name}: rules crashed: {failed}"


def test_rule_ids_are_unique() -> None:
    names = [spec.name for spec in all_rules()]
    assert len(names) == len(set(names))


def test_report_serialises_to_json(gps_jpeg: Path) -> None:
    import json

    payload = json.dumps(run(gps_jpeg).to_dict(), default=str)
    restored = json.loads(payload)
    assert restored["device"]["model"] == "TestCam 900"
    assert restored["verdicts"]["privacy"]["label"]
    assert "raw" not in restored

    with_raw = run(gps_jpeg).to_dict(include_raw=True)
    assert with_raw["raw"]


def test_findings_sort_worst_first(gps_jpeg: Path) -> None:
    ranks = [f.severity.rank for f in run(gps_jpeg).sorted_findings]
    assert ranks == sorted(ranks, reverse=True)


# --------------------------------------------------------- the user's own data


@pytest.mark.samples
def test_real_samples_analyse_cleanly(real_samples: list[Path]) -> None:
    """Every real photo must analyse without a rule crashing.

    No assertion is made about *what* they contain: a stripped file that came
    back through a messenger is a perfectly valid thing to find in someone's
    collection, and the tool's job is to report that honestly.
    """
    for path in real_samples:
        report = run(path)
        assert not [f for f in report.findings if f.id.startswith("internal.")]
        assert report.verdicts["structure"].level is VerdictLevel.GOOD
        assert report.file.sha256
        assert report.tag_count > 0


@pytest.mark.samples
def test_real_iphone_is_recognised_as_original(real_samples: list[Path]) -> None:
    for path in real_samples:
        report = run(path)
        if report.device.make != "Apple":
            continue
        assert report.device.has_makernotes
        assert report.device.os.startswith("iOS")
        assert report.verdicts["originality"].level is VerdictLevel.GOOD


# ------------------------------------------------- reading a stripped file


def test_a_repackaged_file_is_not_reported_as_re_compressed(
    tmp_path: Path, camera_jpeg: Path
) -> None:
    """The distinction the recovery rules exist to draw.

    exiftool rewrites the metadata and copies the compressed data through, so
    the picture is untouched. Calling that a re-encode would tell the reader
    their photo had been degraded when it has not.
    """
    import subprocess

    target = tmp_path / "repackaged.jpg"
    target.write_bytes(camera_jpeg.read_bytes())
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-Make=", "-Model=", str(target)],
        check=True,
        capture_output=True,
    )
    ids = finding_ids(run(target))
    assert "recovery.encoder_library" in ids or "recovery.encoder_vendor" in ids


def test_a_screenshot_is_told_apart_from_a_stripped_photo(tmp_path: Path, blank_jpeg: Path) -> None:
    """Absent because removed, or absent because it never existed.

    Listing "no location" on a screenshot sends somebody looking for a
    coordinate that has never existed anywhere.
    """
    import subprocess

    target = tmp_path / "shot.jpg"
    target.write_bytes(blank_jpeg.read_bytes())
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-UserComment=Screenshot", str(target)],
        check=True,
        capture_output=True,
    )
    assert "recovery.screenshot" in finding_ids(run(target))


def test_an_ordinary_photo_is_not_called_a_screenshot(camera_jpeg: Path) -> None:
    assert "recovery.screenshot" not in finding_ids(run(camera_jpeg))


def test_a_camera_original_gets_no_recovery_findings(camera_jpeg: Path) -> None:
    """These rules are for files with nothing left. A named camera has plenty.

    Firing them on an intact photo would bury the findings that matter under
    structural trivia the reader did not need.
    """
    ids = finding_ids(run(camera_jpeg))
    assert not {i for i in ids if i.startswith("recovery.") and i != "recovery.filename_time"}


# --------------------------------------------------- ai: caption vs signature


def _tagged(source: Path, target: Path, *tags: str) -> Path:
    import subprocess

    target.write_bytes(source.read_bytes())
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", *tags, str(target)],
        check=True,
        capture_output=True,
    )
    return target


def test_a_caption_about_the_gemini_observatory_is_not_an_ai_accusation(
    tmp_path: Path, camera_jpeg: Path
) -> None:
    """The most damaging sentence the tool can print, fired by ordinary prose.

    A real photograph captioned with the name of an observatory was being told,
    at WARNING, that its metadata named an AI image tool.
    """
    target = _tagged(
        camera_jpeg,
        tmp_path / "observatory.jpg",
        "-XMP-dc:Description=Sunrise over the Gemini Observatory",
        "-ExifIFD:UserComment=magnetic flux experiment",
    )
    ids = finding_ids(run(target))
    assert "ai.generator_signature" not in ids
    assert "ai.generator_mentioned" not in ids


def test_spanish_la_imagen_does_not_match_imagen(tmp_path: Path, camera_jpeg: Path) -> None:
    """Word boundaries. "la imagen" is not Google Imagen."""
    target = _tagged(
        camera_jpeg,
        tmp_path / "madrid.jpg",
        "-XMP-dc:Description=La imagen fue tomada en Madrid",
    )
    assert not {i for i in finding_ids(run(target)) if i.startswith("ai.")}


def test_a_signing_field_still_fires_at_warning(tmp_path: Path, camera_jpeg: Path) -> None:
    target = _tagged(camera_jpeg, tmp_path / "signed.jpg", "-IFD0:Software=Midjourney v6")
    finding = next(f for f in run(target).findings if f.id == "ai.generator_signature")
    assert finding.severity is Severity.WARNING
    assert "Midjourney" in finding.params["names"]


def test_a_caption_naming_midjourney_is_a_notice(tmp_path: Path, camera_jpeg: Path) -> None:
    """Unambiguous name, ambiguous field: report it, quietly."""
    target = _tagged(
        camera_jpeg, tmp_path / "caption.jpg", "-XMP-dc:Description=made in Midjourney"
    )
    ids = finding_ids(run(target))
    assert "ai.generator_signature" not in ids
    finding = next(f for f in run(target).findings if f.id == "ai.generator_mentioned")
    assert finding.severity is Severity.NOTICE
    assert finding.confidence is Confidence.LOW


# ------------------------------------------------------- extraction integrity


def test_an_incomplete_extraction_cannot_produce_a_clean_privacy_verdict(
    monkeypatch: pytest.MonkeyPatch, blank_jpeg: Path
) -> None:
    """A tool that stopped looking must not report that it found nothing.

    ``blank_jpeg`` carries no metadata at all, so every axis would otherwise be
    reassuring — which is exactly the file where a half-finished read is most
    dangerous.
    """
    clean = run(blank_jpeg)
    assert clean.verdicts["privacy"].level is not VerdictLevel.UNKNOWN

    real_read = ExifTool.read

    def truncated(self: ExifTool, path: Path, validate: bool = True):  # type: ignore[no-untyped-def]
        meta = real_read(self, path, validate=validate)
        meta.incomplete = (137, 1, 4)
        return meta

    monkeypatch.setattr(ExifTool, "read", truncated)
    report = run(blank_jpeg)
    assert "structural.extraction_incomplete" in finding_ids(report)
    assert report.verdicts["privacy"].level is VerdictLevel.UNKNOWN
    assert report.verdicts["originality"].level is VerdictLevel.UNKNOWN


def test_an_editor_naming_windows_is_not_an_operating_system(
    tmp_path: Path, camera_jpeg: Path
) -> None:
    """Two adjacent rows of one table, one of them false.

    "System: Adobe Photoshop 24.0 (Windows)" sat directly above
    "Editor: Adobe Photoshop".
    """
    for software in (
        "Adobe Photoshop 24.0 (Windows)",
        "Adobe Photoshop Lightroom Classic 12.0 (Windows)",
        "Windows Photo Editor 10.0.10011.16384",
    ):
        target = _tagged(camera_jpeg, tmp_path / "edited.jpg", f"-IFD0:Software={software}")
        assert run(target).device.os is None, software

    target = _tagged(camera_jpeg, tmp_path / "phone.jpg", "-IFD0:Software=Windows Phone 8.1")
    assert run(target).device.os == "Windows Phone 8.1"


def test_accuracy_prose_does_not_claim_four_decimals(tmp_path: Path, camera_jpeg: Path) -> None:
    """ "about 21.8535 metres" — "about" and a tenth of a millimetre, in one sentence."""
    from findpic.i18n import Translator

    target = _tagged(
        camera_jpeg,
        tmp_path / "precise.jpg",
        "-GPSLatitude=48.8584",
        "-GPSLatitudeRef=N",
        "-GPSLongitude=2.2945",
        "-GPSLongitudeRef=E",
        "-GPSHPositioningError=21.8535",
    )
    finding = next(f for f in run(target).findings if f.id == "privacy.gps_location")
    detail = finding.detail(Translator("en"))
    assert "21.8535" not in detail
    assert "22 metres" in detail


# ------------------------------------------------------- the printed commands


REMEDIATION_FIXTURES = [
    "identity_jpeg",
    "text_jpeg",
    "ids_jpeg",
    "gps_jpeg",
    "named_people_jpeg",
    "shadowed_jpeg",
    "gps_png",
]


@pytest.mark.parametrize("fixture", REMEDIATION_FIXTURES)
def test_remediation_actually_removes_what_the_finding_reported(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Run every printed command and check the finding is gone afterwards.

    Nothing had ever executed one. The whole coverage was two substring
    assertions, which is how four commands shipped deleting a subset of their
    own rule's tags, one shipped that zsh refuses, and eleven shipped that
    cannot run on anything but a JPEG.
    """
    import shlex
    import shutil
    import subprocess

    source = request.getfixturevalue(fixture)
    path = tmp_path / source.name
    shutil.copy(source, path)

    before = run(path)
    removals = [f for f in before.findings if f.remediation and f.remediation_kind == "remove"]
    assert removals, f"{fixture} produced no removal command to test"

    for finding in removals:
        argv = shlex.split(finding.remediation)  # asserts it parses as one command
        assert argv[0] == "exiftool"
        assert argv[-1] == str(path), "the command must name the real file"
        assert "-overwrite_original" not in argv

        written = Path(argv[argv.index("-o") + 1])
        written.unlink(missing_ok=True)
        result = subprocess.run(argv, capture_output=True, text=True)
        assert result.returncode == 0, f"{finding.id}: {result.stderr}"
        assert written.exists(), f"{finding.id} wrote nothing"

        after = finding_ids(run(written))
        assert finding.id not in after, f"{finding.id} survived its own fix: {finding.remediation}"


@pytest.mark.parametrize("fixture", REMEDIATION_FIXTURES)
def test_the_combined_fix_removes_every_privacy_finding(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """One command, one output file, every leak that offered a fix.

    Printed separately they could not be run in sequence: each read the original
    and each wrote `clean_copy.jpg`, so the first succeeded and the rest exited 1
    with "already exists" — leaving a file the tool said it had cleaned.
    """
    import shlex
    import shutil
    import subprocess

    from findpic.analysis import fixcmd

    source = request.getfixturevalue(fixture)
    path = tmp_path / source.name
    shutil.copy(source, path)

    before = run(path)
    args = tuple(
        dict.fromkeys(
            arg
            for f in before.findings
            if f.category is Category.PRIVACY and f.remediation_kind == "remove"
            for arg in f.remediation_args
        )
    )
    assert args
    command = fixcmd.command(str(path), before.file.file_type_extension, args)
    argv = shlex.split(command)
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    written = Path(argv[argv.index("-o") + 1])
    left = {f.id for f in run(written).findings if f.category is Category.PRIVACY and f.remediation}
    assert not left, f"the combined command left {left}"


def test_a_remediation_is_a_parseable_argv(tmp_path: Path, camera_jpeg: Path) -> None:
    """A filename must never become shell syntax.

    `IMG_20230813_145435"; id #.jpg` made findpic print a line that ran `id`
    when pasted, and $(…), backticks and $HOME all expanded inside the double
    quotes it used.
    """
    import shlex
    import shutil

    hostile = tmp_path / 'IMG_20230813_145435"; id #$(whoami)`id`$HOME.jpg'
    shutil.copy(camera_jpeg, hostile)

    for finding in run(hostile).findings:
        if not finding.remediation:
            continue
        argv = shlex.split(finding.remediation)
        assert argv[0] == "exiftool"
        # The path survives as exactly one token, whole and unaltered.
        assert str(hostile) in argv, finding.remediation
        assert not any(token in ("id", ";", "&&", "|") for token in argv)


def test_no_remediation_contains_an_unquoted_glob(gps_jpeg: Path) -> None:
    """zsh refuses `-offsettime*=` outright; bash+nullglob deletes the word and
    copies the file unchanged at exit 0, which is the more dangerous of the two."""
    import shlex

    for finding in run(gps_jpeg).findings:
        if not finding.remediation:
            continue
        for token in shlex.split(finding.remediation):
            assert not set(token) & set("*?[~"), f"{finding.id}: {token}"


def test_the_named_people_fix_leaves_the_restore_marker(
    tmp_path: Path, named_people_jpeg: Path
) -> None:
    """One command in the tool used to defeat a safeguard another part provides.

    `-xmp:all=` took the whole packet, and findpic's own restore marker lives in
    it — so a file findpic had restored graded ORIGINAL after running the fix
    findpic printed for it.
    """
    import shlex
    import shutil
    import subprocess

    path = tmp_path / "people.jpg"
    shutil.copy(named_people_jpeg, path)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-q",
            "-XMP-xmpMM:HistoryAction=metadata_restored",
            "-XMP-xmpMM:HistorySoftwareAgent=findpic",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    finding = next(f for f in run(path).findings if f.id == "privacy.named_people")
    argv = shlex.split(finding.remediation or "")
    assert subprocess.run(argv, capture_output=True).returncode == 0

    written = Path(argv[argv.index("-o") + 1])
    after = run(written)
    assert "privacy.named_people" not in finding_ids(after)
    assert "authenticity.xmp_history" in finding_ids(after), "the restore marker was destroyed"


def test_a_fix_names_the_file_it_is_printed_under(tmp_path: Path, gps_png: Path) -> None:
    """Every command hard-coded `photo.jpg` in and `clean_copy.jpg` out.

    On a PNG — or a HEIC — that output name is fatal: exiftool refuses with
    "Can't create JPEG files from other types", exit 1, nothing written.
    """
    import shlex
    import shutil

    path = tmp_path / "shot.png"
    shutil.copy(gps_png, path)
    finding = next(f for f in run(path).findings if f.id == "privacy.gps_location")
    argv = shlex.split(finding.remediation or "")
    assert argv[-1] == str(path)
    assert argv[argv.index("-o") + 1].endswith(".png")
    assert "photo.jpg" not in (finding.remediation or "")
