"""Tests for the command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from findpic.cli import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, collect_paths, main
from findpic.exif import ExifTool

pytestmark = pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")

OFFLINE = ["--no-geocode"]


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_ERROR
    assert "usage: findpic" in capsys.readouterr().out


def test_report_renders_the_expected_sections(
    camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(camera_jpeg), *OFFLINE])
    output = capsys.readouterr().out
    assert code in (EXIT_OK, EXIT_FINDINGS)
    assert "TestCam 900" in output
    assert "ORIGINALITY" in output.upper()
    assert "DEVICE" in output.upper()


def test_unremarkable_file_exits_zero(blank_jpeg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A file with nothing to report must be a clean exit for scripts."""
    code = main([str(blank_jpeg), *OFFLINE])
    capsys.readouterr()
    assert code == EXIT_OK


def test_notable_findings_exit_one(gps_jpeg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main([str(gps_jpeg), *OFFLINE])
    capsys.readouterr()
    assert code == EXIT_FINDINGS


def test_missing_file_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "nope.jpg"), *OFFLINE]) == EXIT_ERROR


def test_json_output_is_valid(gps_jpeg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main([str(gps_jpeg), "--json", *OFFLINE])
    payload = json.loads(capsys.readouterr().out)
    assert payload["device"]["model"] == "TestCam 900"
    assert payload["location"]["latitude"] == pytest.approx(48.8584, abs=1e-3)
    assert set(payload["verdicts"]) == {"originality", "privacy", "structure"}
    assert "raw" not in payload


def test_json_with_raw_includes_tags(gps_jpeg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main([str(gps_jpeg), "--json", "--raw", *OFFLINE])
    payload = json.loads(capsys.readouterr().out)
    assert payload["raw"]


def test_multiple_files_produce_a_json_list(
    camera_jpeg: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main([str(camera_jpeg), str(gps_jpeg), "--json", *OFFLINE])
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 2


def test_summary_prints_one_line_per_file(
    camera_jpeg: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main([str(camera_jpeg), str(gps_jpeg), "--summary", *OFFLINE])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert "camera.jpg" in lines[0]


def test_quiet_hides_informational_findings(
    edited_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`edited.jpg` carries an INFO finding about surviving camera details."""
    main([str(edited_jpeg), *OFFLINE])
    verbose = capsys.readouterr().out
    main([str(edited_jpeg), "--quiet", *OFFLINE])
    quiet = capsys.readouterr().out

    assert "survived the trip" in verbose
    assert "survived the trip" not in quiet
    # The warnings that justify the verdict must still be there.
    assert "Adobe Photoshop" in quiet


def test_no_geocode_makes_no_network_call(
    gps_jpeg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-geocode must be airtight; this is the flag people rely on."""
    import urllib.request

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted with --no-geocode")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    assert main([str(gps_jpeg), "--json", *OFFLINE]) in (EXIT_OK, EXIT_FINDINGS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["location"]["place"] is None


def test_directory_is_expanded(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    album = tmp_path / "album"
    album.mkdir()
    (album / "one.jpg").write_bytes(camera_jpeg.read_bytes())
    (album / "notes.txt").write_text("ignore me")

    main([str(album), "--summary", *OFFLINE])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "one.jpg" in lines[0]


def test_recursive_walks_subdirectories(tmp_path: Path, camera_jpeg: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "deep.jpg").write_bytes(camera_jpeg.read_bytes())

    assert len(collect_paths([tmp_path], recursive=False)) == 0
    assert len(collect_paths([tmp_path], recursive=True)) == 1


def test_no_color_env_is_respected(
    camera_jpeg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    main([str(camera_jpeg), *OFFLINE])
    assert "\x1b[" not in capsys.readouterr().out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "findpic" in capsys.readouterr().out
