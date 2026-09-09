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
    """On stderr: it is an error, and on stdout it landed in whatever was
    reading the report."""
    assert main([]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "usage: findpic" in captured.err
    assert captured.out == ""


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
    # Always a list, one entry per file requested — see test_json_is_always_a_list.
    payload = json.loads(capsys.readouterr().out)[0]
    assert payload["device"]["model"] == "TestCam 900"
    assert payload["location"]["latitude"] == pytest.approx(48.8584, abs=1e-3)
    assert set(payload["verdicts"]) == {"originality", "privacy", "structure"}
    assert "raw" not in payload


def test_json_with_raw_includes_tags(gps_jpeg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main([str(gps_jpeg), "--json", "--raw", *OFFLINE])
    # Always a list, one entry per file requested — see test_json_is_always_a_list.
    payload = json.loads(capsys.readouterr().out)[0]
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
    # Always a list, one entry per file requested — see test_json_is_always_a_list.
    payload = json.loads(capsys.readouterr().out)[0]
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

    # --summary piped is the machine-readable mode, and there the path is
    # emitted exactly as it is on disk so that `cut -f2 | xargs` names the real
    # file — the same bargain ls and find make when their output is not a
    # terminal. Every other column is still sanitised, and the aligned mode a
    # person actually reads sanitises all five.
    main([str(target), "--summary", *OFFLINE])
    row = capsys.readouterr().out.rstrip("\n")
    columns = row.split("\t")
    assert len(columns) == 5
    assert columns[1] == str(target), "the path must be usable"
    for index, column in enumerate(columns):
        if index == 1:
            continue
        assert "\x1b" not in column, f"column {index}: {column!r}"


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


def _hostile_dir(tmp_path: Path, source: Path) -> Path:
    """Copies of one photograph under names a filesystem allows and a terminal
    should never be handed."""
    import os
    import shutil

    room = tmp_path / "hostile"
    room.mkdir()
    shutil.copy(source, room / "base.jpg")
    shutil.copy(source, room / os.fsdecode(b"bad\xff\xfe.jpg"))
    for name in ("car\rriage.jpg", "es\x1b[31mc.jpg", "bidi‮gpj.exe.jpg", "new\nline.jpg"):
        shutil.copy(source, room / name)
    return room


def test_summary_survives_every_name_a_filesystem_allows(
    tmp_path: Path, camera_jpeg: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    """One file, one line, five columns — including a name that is not valid
    UTF-8, which used to kill the run part-way through a listing.

    Captured at the file descriptor and as bytes, because the path is written as
    bytes: a filename is not required to be text.
    """
    import os

    room = _hostile_dir(tmp_path, camera_jpeg)
    expected = len(list(os.scandir(room)))
    assert main([str(room), "--summary", *OFFLINE]) in (EXIT_OK, EXIT_FINDINGS)
    rows = [line for line in capfdbinary.readouterr().out.split(b"\n") if line]
    assert len(rows) == expected
    for row in rows:
        assert len(row.split(b"\t")) == 5, repr(row)


def test_every_summary_path_names_a_real_file(
    tmp_path: Path, camera_jpeg: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    """The piped mode exists to be fed into `cut -f2 | xargs`, so the path is
    emitted as it is on disk — only the newline and NUL that would break the
    record format itself are escaped."""
    import os

    room = _hostile_dir(tmp_path, camera_jpeg)
    main([str(room), "--summary", *OFFLINE])
    for row in capfdbinary.readouterr().out.split(b"\n"):
        if not row:
            continue
        path = os.fsdecode(row.split(b"\t")[1])
        real = path.replace("\\n", "\n").replace("\\0", "\0").replace("\\\\", "\\")
        assert os.path.exists(real), f"unusable path: {path!r}"


def test_force_color_does_not_defeat_the_piped_form(
    tmp_path: Path,
    camera_jpeg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """rich reports a terminal whenever FORCE_COLOR is set — the normal state on
    CI runners — so `--summary | awk -F'\\t'` got coloured fixed-width basenames
    and not one tab."""
    import shutil

    monkeypatch.setenv("FORCE_COLOR", "1")
    target = tmp_path / "photo.jpg"
    shutil.copy(camera_jpeg, target)
    main([str(target), "--summary", *OFFLINE])
    out = capsys.readouterr().out
    assert out.count("\t") == 4, repr(out)
    assert "\x1b" not in out


@pytest.mark.parametrize("width", ["-5", "0", "3"])
def test_an_impossible_width_is_refused(
    width: str, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It silently produced zero rows and still exited 1, which is
    indistinguishable from a run that found something."""
    assert main([str(camera_jpeg), "--summary", "--width", width, *OFFLINE]) == EXIT_ERROR
    assert "--width" in capsys.readouterr().err


def test_the_place_column_is_never_silently_shortened(
    gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cropping turned "48.858400, 2.294500" into "48.858400, 2.2" — a
    well-formed coordinate fifty kilometres from the truth."""
    main([str(gps_jpeg), "--summary", "--width", "60", *OFFLINE])
    row = capsys.readouterr().out.rstrip("\n")
    coordinates = row.split("\t")[-1] if "\t" in row else row
    assert "48.858400" in coordinates or "…" in coordinates


def test_a_command_runs_on_a_file_whose_name_fights_back(
    tmp_path: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed command must both run and be safe to print.

    shlex.quote is right for the shell and wrong for the screen — it wraps a
    newline in quotes and passes the byte through — while sanitising the
    finished command rewrites the path *inside* those quotes, so it names a file
    that does not exist, or one that does and is the wrong one.
    """
    import shutil
    import subprocess

    for name in ("nl\nname.jpg", "esc\x1b[41m.jpg", "-rf.jpg", "it's here.jpg"):
        target = tmp_path / name
        shutil.copy(gps_jpeg, target)
        main([str(target), "--width", "200", *OFFLINE])
        printed = [
            line.strip()
            for line in capsys.readouterr().out.splitlines()
            if line.strip().startswith("fix: exiftool")
        ]
        assert printed, name
        written = target.with_name(f"{target.stem}.clean{target.suffix}")
        for line in printed:
            command = line[len("fix: ") :]
            assert not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in command), repr(command)
            # Every fix for one file writes the same <stem>.clean.<ext>, which
            # is why the report also prints a single combined command; clear it
            # between runs so each is tested on its own.
            written.unlink(missing_ok=True)
            run = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
            assert run.returncode == 0, f"{name}: {run.stderr}"
            assert written.exists(), f"{name}: wrote nothing"
        for leftover in tmp_path.glob("*"):
            leftover.unlink()


def test_a_filename_cannot_forge_a_hyperlink_in_the_write_path(
    tmp_path: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--clean/--backup/--restore interpolate a name into a string rich parses
    as markup, so a photograph could emit an OSC 8 link of its own choosing."""
    import shutil

    target = tmp_path / "mk[link=http:evil.example]CLICK[red]x.jpg"
    shutil.copy(gps_jpeg, target)
    assert main([str(target), "--clean", *OFFLINE]) == EXIT_OK
    out = capsys.readouterr().out
    assert "evil.example" in out, "the name must be printed as it is"
    assert "\x1b" not in out


def test_the_c1_control_range_is_closed(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """U+009B is a single-character CSI and U+009D an OSC: on a terminal reading
    Latin-1 they open a sequence exactly as ESC-[ and ESC-] do."""
    import shutil
    import subprocess

    target = tmp_path / "c1.jpg"
    shutil.copy(camera_jpeg, target)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-Software=\u009b31mRED", str(target)],
        check=True,
        capture_output=True,
    )
    main([str(target), *OFFLINE])
    out = capsys.readouterr().out
    assert not any("\u0080" <= c <= "\u009f" for c in out)


def test_the_tag_group_list_is_not_capped(
    camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--notes exists to list every namespace; the 140-cell value cap dropped the
    tail with no "+N more" to say so."""
    main([str(camera_jpeg), "--notes", "--width", "200", *OFFLINE])
    row = next(line for line in capsys.readouterr().out.splitlines() if "Tag groups" in line)
    assert "…" not in row


# --------------------------------------------------------- the JSON interface


def test_json_is_always_a_list(
    tmp_path: Path, camera_jpeg: Path, gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It was a bare object when exactly one file *succeeded*, so
    `findpic *.jpg --json | jq '.[].file.name'` worked all week and broke the
    morning the glob matched one file."""
    main([str(camera_jpeg), "--json", *OFFLINE])
    one = json.loads(capsys.readouterr().out)
    assert isinstance(one, list) and len(one) == 1

    main([str(camera_jpeg), str(gps_jpeg), "--json", *OFFLINE])
    two = json.loads(capsys.readouterr().out)
    assert isinstance(two, list) and len(two) == 2


def test_a_failed_file_appears_in_the_json(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure reached stderr only, so a consumer could not tell "no GPS"
    from "never read"."""
    import shutil

    good = tmp_path / "good.jpg"
    shutil.copy(camera_jpeg, good)
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"")

    assert main([str(good), str(bad), "--json", *OFFLINE]) == EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    failed = [entry for entry in payload if "error" in entry]
    assert len(failed) == 1
    assert failed[0]["file"]["path"] == str(bad)


def test_ndjson_emits_one_object_per_line(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    for index in range(3):
        shutil.copy(camera_jpeg, tmp_path / f"{index}.jpg")
    main([str(tmp_path), "--ndjson", *OFFLINE])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 3
    for line in lines:
        assert json.loads(line)["file"]["path"]


def test_verdict_reason_ids_join_against_finding_ids(
    gps_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`reasons` is param-interpolated prose that changes with --lang; nothing
    mapped a verdict back to the findings that produced it."""
    main([str(gps_jpeg), "--json", "--lang", "uk", *OFFLINE])
    report = json.loads(capsys.readouterr().out)[0]
    ids = {finding["id"] for finding in report["findings"]}
    # A clean axis legitimately cites nothing; what matters is that whatever it
    # does cite can be joined back to a finding.
    assert any(v["reason_ids"] for v in report["verdicts"].values())
    for verdict in report["verdicts"].values():
        assert set(verdict["reason_ids"]) <= ids, verdict["axis"]


# ------------------------------------------------------------ giving up, and
# ------------------------------------------------------------ finding nothing


def test_interrupt_exits_cleanly(
    tmp_path: Path,
    camera_jpeg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 300-file scan takes the better part of a minute, so this happens often,
    and it ended in a raw traceback that reads as a crash."""
    import shutil

    from findpic import cli

    for index in range(3):
        shutil.copy(camera_jpeg, tmp_path / f"{index}.jpg")

    real = cli.analyze
    seen = {"n": 0}

    def flaky(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] == 2:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "analyze", flaky)
    assert main([str(tmp_path), *OFFLINE]) == 130
    captured = capsys.readouterr()
    assert "Interrupted" in captured.err
    assert "1 of 3" in captured.err
    assert "Traceback" not in captured.err


def test_a_directory_of_subdirectories_suggests_recursive(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A photo library organised in folders — the normal shape of one — said
    "No image files found" with three hundred images sitting right there."""
    import shutil

    album = tmp_path / "album"
    (album / "2023").mkdir(parents=True)
    shutil.copy(camera_jpeg, album / "2023" / "one.jpg")

    assert main([str(album), *OFFLINE]) == EXIT_ERROR
    error = capsys.readouterr().err
    assert "--recursive" in error
    assert "1" in error


def test_an_unrecognised_suffix_is_reported_not_ignored(
    tmp_path: Path, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A .jfif was skipped by a directory scan and analysed fine when named
    directly, which looks like the file is the problem."""
    import shutil

    shutil.copy(camera_jpeg, tmp_path / "photo.jpg")
    (tmp_path / "notes.txt").write_text("x")
    main([str(tmp_path), *OFFLINE])
    assert ".txt" in capsys.readouterr().err


def test_a_jfif_is_recognised(tmp_path: Path, camera_jpeg: Path) -> None:
    """It is what Chrome and Outlook save."""
    import shutil

    shutil.copy(camera_jpeg, tmp_path / "chrome.jfif")
    assert collect_paths([tmp_path], recursive=False) == [tmp_path / "chrome.jfif"]


# ------------------------------------------------------------- the geocoder


def test_a_malformed_geocode_payload_does_not_abort_the_run(
    tmp_path: Path,
    gps_jpeg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cache is plain JSON in ~/.cache and was replayed unvalidated, so a
    payload whose `address` is a list raised AttributeError with no network
    involved at all — and took the whole run with it, stdout empty."""
    import shutil

    cache = tmp_path / "geocode.json"
    cache.write_text('{"48.8584,2.2945@en": {"address": ["not", "a", "dict"]}}')
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / "findpic").mkdir(exist_ok=True)
    shutil.copy(cache, tmp_path / "findpic" / "geocode.json")

    for index in range(2):
        shutil.copy(gps_jpeg, tmp_path / f"{index}.jpg")
    code = main([str(tmp_path / "0.jpg"), str(tmp_path / "1.jpg"), "--json"])
    assert code in (EXIT_OK, EXIT_FINDINGS)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2, "one bad cache entry must not lose the other file"


def test_a_429_stops_further_geocoding(tmp_path: Path) -> None:
    """A local server answering 429 with Retry-After: 3600 received ten more
    requests — a good way to have the user's address banned by the service
    findpic depends on."""
    import urllib.error

    from findpic.geocode import Geocoder

    calls = {"n": 0}

    def refuse(latitude: float, longitude: float) -> dict:
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "3600"}, None)

    geocoder = Geocoder(cache_file=tmp_path / "c.json")
    geocoder._fetch = refuse  # type: ignore[method-assign]
    for index in range(10):
        geocoder.reverse(48.0 + index * 0.01, 2.0)
    assert calls["n"] == 1
    assert geocoder.gave_up and "3600" in geocoder.gave_up


def test_a_dead_network_is_not_retried_forever(tmp_path: Path) -> None:
    """Every failure branch returned without touching the cache, so a dead
    network cost the full timeout per photograph — 49.5 s for six photos."""
    import urllib.error

    from findpic.geocode import Geocoder

    calls = {"n": 0}

    def dead(latitude: float, longitude: float) -> dict:
        calls["n"] += 1
        raise urllib.error.URLError("Connection refused")

    geocoder = Geocoder(cache_file=tmp_path / "c.json")
    geocoder._fetch = dead  # type: ignore[method-assign]
    for index in range(10):
        geocoder.reverse(48.0 + index * 0.01, 2.0)
    assert calls["n"] == 3, calls


def test_an_oversized_geocode_body_is_rejected(tmp_path: Path) -> None:
    """A 25 MB reply was read whole, parsed, and written verbatim into the cache
    and into place_detail, with the report looking entirely normal."""
    import io
    import urllib.request

    from findpic.geocode import MAX_RESPONSE_BYTES, Geocoder

    class Flood(io.BytesIO):
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def flood(request: object, timeout: float = 0) -> Flood:
        return Flood(b'{"address": {"city": "' + b"x" * (MAX_RESPONSE_BYTES * 2) + b'"}}')

    geocoder = Geocoder(cache_file=tmp_path / "c.json")
    original = urllib.request.urlopen
    urllib.request.urlopen = flood  # type: ignore[assignment]
    try:
        place, reason = geocoder.reverse(48.8584, 2.2945)
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]
    assert place is None
    assert reason and "could not read" in reason
    assert not (tmp_path / "c.json").exists()


def test_summary_rows_are_printed_as_each_file_finishes(tmp_path: Path, camera_jpeg: Path) -> None:
    """Buffered, the first byte of a 100-file run arrived at 22.5 s, so
    `findpic album -r --summary | head -5` cost the whole scan and a long run
    was indistinguishable from a hang."""
    import shutil
    import subprocess
    import sys

    for index in range(12):
        shutil.copy(camera_jpeg, tmp_path / f"{index}.jpg")

    code = (
        "import sys; from findpic.cli import main; "
        "sys.exit(main([sys.argv[1], '--summary', '--no-geocode']))"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        # A row must be readable before the process has exited.
        first = proc.stdout.readline()  # type: ignore[union-attr]
        assert first, "nothing was written before the run finished"
        assert proc.poll() is None or first.count(b"\n") == 1
    finally:
        proc.stdout.read()  # type: ignore[union-attr]
        proc.wait()


@pytest.mark.parametrize(
    ("flags", "needle"),
    [
        (["--raw"], "--raw"),
        (["--force"], "--force"),
        (["--timeout", "-5"], "--timeout"),
    ],
)
def test_a_flag_that_does_nothing_is_an_error(
    flags: list[str], needle: str, camera_jpeg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All three were accepted, did nothing, and exited 0 — except --timeout,
    which was accepted and then failed every file with "did not finish within
    -5s"."""
    with pytest.raises(SystemExit) as exit_info:
        main([str(camera_jpeg), *flags, *OFFLINE])
    assert exit_info.value.code == 2
    assert needle in capsys.readouterr().err


def test_help_documents_the_exit_status(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit codes are the scripting contract and appeared only in the README."""
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "Exit status" in out
    assert "130" in out


def test_version_names_the_exiftool_build(capsys: pytest.CaptureFixture[str]) -> None:
    """Every tag findpic can read comes from exiftool's database, so the
    findpic version alone does not identify what produced a report."""
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "exiftool" in capsys.readouterr().out
