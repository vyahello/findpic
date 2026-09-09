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


@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", ("36 mm equivalent", "1/250 s")), ("uk", ("еквівалент 36 мм", "1/250 с"))],
)
def test_units_are_translated_not_baked_in(
    camera_jpeg: Path,
    capsys: pytest.CaptureFixture[str],
    language: str,
    expected: tuple[str, ...],
) -> None:
    """Every unit the CLI prints must come from the catalogue.

    Focal lengths and shutter speeds are stored as bare numbers so each renderer
    can attach its own translated unit. Twice now a renderer has forgotten its
    half of that: the lens line lost its "mm" entirely, and the exposure line
    kept a hard-coded English "s" in the Ukrainian report. Both were invisible
    without reading the output side by side, which is what this does.
    """
    main([str(camera_jpeg), "--lang", language, *OFFLINE])
    output = capsys.readouterr().out
    for fragment in expected:
        assert fragment in output


def test_summary_legend_goes_to_stderr(
    camera_jpeg: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The glyph key must reach a human without reaching a pipe.

    --summary exists to be piped into grep and awk, so a legend on stdout would
    corrupt the thing the mode is for. It was missing entirely for a while, which
    left three unexplained glyph columns and a README documenting a key nobody
    ever printed.
    """
    main([str(camera_jpeg), str(gps_jpeg), "--summary", *OFFLINE])
    captured = capsys.readouterr()
    # Asserted on the glyph vocabulary rather than the wording: the legend has
    # to fit eighty cells, so the prose is allowed to change.
    assert "+ good" in captured.err
    assert "+ good" not in captured.out
    assert captured.out.count("\n") == 2


def test_an_unreadable_file_exits_2_and_prints_no_verdict(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one thing worse than no answer is a reassuring wrong one."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 file")
    target = tmp_path / "locked.jpg"
    target.write_bytes(camera_jpeg.read_bytes())
    target.chmod(0o000)
    try:
        code = main([str(target), *OFFLINE])
    finally:
        target.chmod(0o644)
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "CLEAN" not in captured.out
    assert target.name in captured.err


def test_a_missing_direction_ref_is_not_reported_as_true_north(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exif has no default for GPSImgDirectionRef; declination reaches +8°."""
    import subprocess

    target = tmp_path / "bearing.jpg"
    target.write_bytes(camera_jpeg.read_bytes())
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-q",
            "-GPSLatitude=48.8584",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=2.2945",
            "-GPSLongitudeRef=E",
            "-GPSImgDirection=349",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    main([str(target), *OFFLINE])
    output = " ".join(capsys.readouterr().out.split())
    assert "349" in output
    assert "true or magnetic north" in output


# ------------------------------------------------------------- WHEN (item C2)


def _named(tmp_path: Path, source: Path, name: str) -> Path:
    """A metadata-free copy under a name a messenger would have given it."""
    import shutil
    import subprocess

    target = tmp_path / name
    shutil.copy(source, target)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-all=", str(target)],
        check=True,
        capture_output=True,
    )
    return target


def test_a_stripped_file_has_no_when_section(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A heading promising when the photo was taken, over the date this copy
    happened to be written to this disk, is worse than no heading."""
    target = _named(tmp_path, camera_jpeg, "nothing-in-the-name.jpg")
    main([str(target), *OFFLINE])
    output = capsys.readouterr().out
    assert " WHEN" not in output
    assert "File saved" not in output


def test_a_whatsapp_filename_puts_the_date_under_when(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _named(tmp_path, camera_jpeg, "IMG-20230813-WA0002.jpg")
    main([str(target), *OFFLINE])
    output = " ".join(capsys.readouterr().out.split())
    assert "WHEN" in output
    assert "2023-08-13" in output.split("FINDINGS")[0]
    assert "time of day not known" in output
    assert "from the filename" in output


def test_a_recovered_date_is_not_styled_like_a_tag(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It must never be mistaken for something the file actually says."""
    target = _named(tmp_path, camera_jpeg, "IMG_20230813_145435.jpg")
    main([str(target), *OFFLINE])
    output = " ".join(capsys.readouterr().out.split())
    assert "2023-08-13 14:54:35" in output
    assert "from the filename, not the metadata" in output


def test_summary_shows_a_recovered_date_rather_than_no_timestamp(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _named(tmp_path, camera_jpeg, "IMG-20230813-WA0002.jpg")
    main([str(target), "--summary", *OFFLINE])
    output = capsys.readouterr().out
    assert "(2023-08-13)" in output
    assert "no timestamp" not in output


def test_the_subsecond_reaches_the_taken_row(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A decimal fraction, not milliseconds: "052" is 52 thousandths and "5" is
    half a second, so the digits go in exactly as recorded."""
    import shutil
    import subprocess

    target = tmp_path / "subsec.jpg"
    shutil.copy(camera_jpeg, target)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-SubSecTimeOriginal=052", str(target)],
        check=True,
        capture_output=True,
    )
    main([str(target), *OFFLINE])
    output = capsys.readouterr().out
    assert "14:30:00.052" in output


# ---------------------------------------------------------- --quiet (item C3)


def test_quiet_keeps_the_screenshot_explanation(
    samples_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both provenance findings are INFO, so the blanket filter removed the one
    sentence that explains why everything else is empty."""
    shot = samples_dir / "22.jpeg"
    if not shot.exists():  # pragma: no cover - the owner's corpus
        pytest.skip("sample not present")
    main([str(shot), "--quiet", *OFFLINE])
    output = " ".join(capsys.readouterr().out.split())
    assert "screen capture" in output


def test_the_provenance_line_is_not_printed_twice(
    samples_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shot = samples_dir / "22.jpeg"
    if not shot.exists():  # pragma: no cover
        pytest.skip("sample not present")
    main([str(shot), *OFFLINE])
    output = capsys.readouterr().out
    assert output.count("This is a screen capture") == 1


def test_quiet_never_leaves_a_verdict_standing_over_nothing(
    tmp_path: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Severity and weight are separate fields, so an INFO finding can still be
    what a verdict is built on. Hiding it left "a few details leak" over an
    empty list — the report asserting a leak and declining to name it."""
    import shutil

    target = tmp_path / "photo.jpg"
    shutil.copy(gps_jpeg, target)
    main([str(target), "--quiet", *OFFLINE])
    output = capsys.readouterr().out

    from findpic.analysis import AnalysisOptions, analyze
    from findpic.models import VerdictLevel

    report = analyze(target, options=AnalysisOptions(geocode=False))
    for verdict in report.verdicts.values():
        if verdict.level.rank <= VerdictLevel.GOOD.rank or not verdict.reasons:
            continue
        assert "FINDINGS" in output, "a non-good verdict with no findings section"


# ------------------------------------------------------------ IMAGE (item C5)


def test_the_file_section_names_the_tag_groups(
    camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Computed every run, stored on the model, in --json, rendered by nobody."""
    main([str(camera_jpeg), *OFFLINE])
    output = capsys.readouterr().out
    assert "Tag groups" in output
    # Scoped to the row itself: "System" is also the DEVICE section's OS label.
    row = next(line for line in output.splitlines() if "Tag groups" in line)
    assert "ExifIFD" in row
    # Filesystem facts and exiftool's own derivations are not the file, and
    # "Other" is exiftool's echo of the path rather than a group at all.
    for absent in ("System", "ExifTool", "Composite", "Other"):
        assert absent not in row, row


def test_a_face_region_says_where_in_the_frame(
    tmp_path: Path, named_people_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    target = tmp_path / "people.jpg"
    shutil.copy(named_people_jpeg, target)
    main([str(target), *OFFLINE])
    output = " ".join(capsys.readouterr().out.split())
    assert "In frame" in output
    assert "of the frame" in output


# ------------------------------------------------------- the report on screen


def test_escape_sequences_never_reach_the_terminal(
    tmp_path: Path, camera_jpeg: Path, magick, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ESC in a photograph is the file's author taking the reader's cursor.

    rich strips the BEL that would *terminate* an OSC and leaves the ESC that
    opens it, and Text.append is markup-inert but not control-character-inert —
    so the header, the findings body and the summary all leaked.
    """
    import subprocess

    target = tmp_path / "esc\x1b[41mname.jpg"
    magick("-size", "64x48", "xc:gray", str(target))
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-q",
            "-Artist=A\x1b]0;HACKED\x07B",
            "-Model=M\x1b[41mX",
            "-LensModel=L\x1b[7mY",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    main([str(target), *OFFLINE])
    assert "\x1b" not in capsys.readouterr().out

    main([str(target), "--summary", *OFFLINE])
    assert "\x1b" not in capsys.readouterr().out


def test_a_bracketed_filename_is_reported_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error lines built rich markup by f-string, so a file named
    `[bold red]OWNED[not a tag].jpg` reported a path that does not exist."""
    target = tmp_path / "[blink bold red]OWNED[not a tag].jpg"
    target.write_bytes(b"")
    assert main([str(target), *OFFLINE]) == EXIT_ERROR
    assert "[blink bold red]OWNED[not a tag].jpg" in capsys.readouterr().err


def test_summary_is_always_one_line_per_file(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One file, one line — always. A newline in a Model forged an extra row."""
    import shutil
    import subprocess

    first, second = tmp_path / "one.jpg", tmp_path / "two.jpg"
    shutil.copy(camera_jpeg, first)
    shutil.copy(camera_jpeg, second)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-q",
            "-Model=Mod\nFORGED-ROW",
            "-Artist=A\x1b[41mX",
            str(first),
        ],
        check=True,
        capture_output=True,
    )
    main([str(first), str(second), "--summary", *OFFLINE])
    out = capsys.readouterr().out
    assert out.count("\n") == 2
    assert "FORGED-ROW" in out  # kept, but on the same line


def test_summary_columns_align_with_a_cjk_filename(
    tmp_path: Path, camera_jpeg: Path, magick, capsys: pytest.CaptureFixture[str]
) -> None:
    """`f"{name:<26.26}"` counts codepoints; a CJK name is two cells each."""
    from rich.cells import cell_len

    ascii_name = tmp_path / "plain-ascii-name.jpg"
    cjk_name = tmp_path / "日本語のファイル.jpg"
    magick("-size", "64x48", "xc:gray", str(ascii_name))
    magick("-size", "64x48", "xc:gray", str(cjk_name))

    main([str(ascii_name), str(cjk_name), "--summary", *OFFLINE])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    starts = {cell_len(line[: line.index("Unknown device")]) for line in lines}
    assert len(starts) == 1, f"device column starts at different cells: {starts}"


def test_summary_prints_the_path_when_piped(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated basename cannot be fed back into any command, and under -r
    two IMG_0001.JPG in different directories are indistinguishable."""
    import shutil

    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    target = nested / "deep.jpg"
    shutil.copy(camera_jpeg, target)
    main([str(target), "--summary", *OFFLINE])
    out = capsys.readouterr().out
    assert str(target) in out
    assert "\t" in out


def test_a_command_is_never_split_across_lines(
    tmp_path: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both halves of a wrapped command look like plausible shell, so pasting
    the pair runs one without its argument and then executes the filename."""
    import shutil

    target = tmp_path / "photo.jpg"
    shutil.copy(gps_jpeg, target)
    main([str(target), "--width", "60", *OFFLINE])
    for line in capsys.readouterr().out.splitlines():
        stripped = line.strip()
        if not stripped.startswith("exiftool "):
            continue
        assert stripped.endswith(str(target)), f"command split: {stripped!r}"


def test_the_report_is_capped_at_a_readable_width(
    camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 200-column terminal gave a 200-cell box around a 24-character title."""
    from findpic.cli import MAX_WIDTH

    main([str(camera_jpeg), "--width", "200", *OFFLINE])
    wide = capsys.readouterr().out
    assert max(len(line) for line in wide.splitlines()) > MAX_WIDTH

    main([str(camera_jpeg), *OFFLINE])
    default = capsys.readouterr().out
    # The header box is the width; command lines are deliberately unwrapped and
    # are allowed past it, because a wrapped command is two broken commands.
    box = next(line for line in default.splitlines() if line.startswith("╭"))
    assert len(box) <= MAX_WIDTH


def test_a_long_value_is_capped_but_a_hash_is_not(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One value, one policy: a 307-character Artist rendered as seven folded
    lines in DEVICE and a 60-character ellipsis in the finding below it."""
    import shutil
    import subprocess

    target = tmp_path / "long.jpg"
    shutil.copy(camera_jpeg, target)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", f"-Artist={'A' * 307}", str(target)],
        check=True,
        capture_output=True,
    )
    main([str(target), *OFFLINE])
    out = capsys.readouterr().out
    assert "A" * 200 not in out.replace("\n", "").replace(" ", "")
    # The hash is the one row where the whole string is the point.
    from findpic.analysis import AnalysisOptions, analyze

    report = analyze(target, options=AnalysisOptions(geocode=False))
    assert report.file.sha256 in out.replace("\n", "").replace(" ", "")


def test_a_bidi_filename_is_shown_as_it_really_is(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """findpic rates a right-to-left override HIGH RISK ten lines below a header
    that was printing the lie it produces."""
    import shutil

    target = tmp_path / "holiday‮gpj.exe.jpg"
    shutil.copy(camera_jpeg, target)
    main([str(target), *OFFLINE])
    out = capsys.readouterr().out
    assert "‮" not in out
    assert "\\u202e" in out
