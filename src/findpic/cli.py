"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.text import Text

from . import __version__
from .analysis import AnalysisOptions, analyze
from .exif import ExifTool, ExifToolError, ExifToolMissing
from .geocode import DEFAULT_TIMEOUT as GEOCODE_TIMEOUT
from .geocode import Geocoder
from .i18n import LANGUAGE_NAMES, Translator, available_languages, detect_language
from .models import Report, Severity, VerdictLevel
from .recover import PRECISION_SECOND, timestamp_from_filename
from .render.terminal import AXES, LEVEL_GLYPH, LEVEL_STYLE, render_report
from .restore import RestoreError, backup, clean, restore
from .util import printable

#: Beyond this the prose gets harder to read, not easier. A terminal wider than
#: this keeps its width for everything else; the report just stops growing.
MAX_WIDTH = 100

#: Narrower than this and there is no room for a label and a value.
MIN_WIDTH = 20

IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".orf",
    ".rw2",
    ".raf",
    ".pef",
    ".srw",
    ".mp4",
    ".mov",
    # What Chrome and Outlook save a JPEG as, and the video containers a phone
    # produces. A directory scan silently ignored them while naming one directly
    # analysed it fine.
    ".jfif",
    ".jfi",
    ".m4v",
    ".3gp",
    ".avi",
    ".mkv",
}

#: Shell exit codes, so findpic composes with scripts and CI.
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

#: The shell's own convention for a process ended by SIGINT.
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="findpic",
        description=(
            "Read a photo's metadata and explain what device made it, when and "
            "where, whether it is an untouched original, and what it leaks."
        ),
        epilog=(
            "Examples:\n"
            "  findpic photo.jpg                  analyse one photo\n"
            "  findpic *.jpg --summary            one line per file\n"
            "  findpic album/ --recursive         walk a directory\n"
            "  findpic photo.jpg --json           machine-readable output\n"
            "  findpic photo.jpg --no-geocode     never touch the network\n"
            "\n"
            "  findpic photo.jpg --clean         write photo.clean.jpg with no metadata\n"
            "\n"
            "Metadata is only restorable if a copy of it exists. Make one first:\n"
            "  findpic photo.jpg --backup               write photo.jpg.mie beside it\n"
            "  findpic stripped.jpg --restore photo.jpg.mie\n"
            "  findpic stripped.jpg --restore original.jpg    (any donor that still has it)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", type=Path, help="image files or directories")
    parser.add_argument("--version", action="version", version=f"findpic {__version__}")

    output = parser.add_argument_group("output")
    output.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    output.add_argument("--raw", action="store_true", help="with --json, include every raw tag")
    output.add_argument(
        "--ndjson",
        action="store_true",
        help="one compact JSON object per line, written as each file finishes",
    )
    output.add_argument("--summary", "-s", action="store_true", help="one line per file")
    output.add_argument("--quiet", "-q", action="store_true", help="hide informational findings")
    output.add_argument("--notes", action="store_true", help="show exiftool's own warnings")
    output.add_argument(
        "--no-color",
        action="store_true",
        # "colour", not "colour and styling": this follows the NO_COLOR
        # convention, which is about colour. Bold survives; hyperlinks do not.
        help="disable colour (also honours NO_COLOR)",
    )
    output.add_argument(
        "--width",
        type=int,
        default=None,
        metavar="COLS",
        help=f"report width (default: the terminal's, capped at {MAX_WIDTH})",
    )
    output.add_argument(
        "--links",
        choices=("auto", "always", "never"),
        default="auto",
        help="clickable terminal hyperlinks (default: auto)",
    )

    metadata = parser.add_argument_group(
        "metadata",
        "Write operations. All three leave every input file exactly as it was.",
    )
    metadata.add_argument(
        "--backup",
        action="store_true",
        help="write a sidecar holding every tag, so it can be restored later",
    )
    metadata.add_argument(
        "--clean",
        action="store_true",
        help="write a metadata-free copy beside the original (photo.heic -> photo.clean.heic)",
    )
    metadata.add_argument(
        "--out",
        metavar="PATH",
        type=Path,
        default=None,
        help="with --clean or --restore: the file to write, or a directory to write into",
    )
    metadata.add_argument(
        "--restore",
        metavar="DONOR",
        type=Path,
        default=None,
        help="copy metadata from a sidecar or an intact image into a new file",
    )
    metadata.add_argument(
        "--force",
        action="store_true",
        help="with --restore, accept a donor whose picture is a different size",
    )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument(
        "--no-geocode",
        action="store_true",
        help="do not resolve coordinates to a place name (no network at all)",
    )
    languages = available_languages()
    behaviour.add_argument(
        "--lang",
        choices=languages,
        default=None,
        metavar="CODE",
        help=(
            "report language and place-name language: "
            + ", ".join(f"{code} ({LANGUAGE_NAMES.get(code, code)})" for code in languages)
            + " (default: from your locale, else en)"
        ),
    )
    behaviour.add_argument(
        "--no-hash", action="store_true", help="skip SHA-256/MD5 (faster on big files)"
    )
    behaviour.add_argument(
        "--recursive", "-r", action="store_true", help="descend into directories"
    )
    behaviour.add_argument(
        "--timeout", type=int, default=60, metavar="SEC", help="per-file exiftool timeout"
    )
    behaviour.add_argument(
        "--geocode-timeout",
        type=float,
        default=None,
        metavar="SEC",
        help=f"how long to wait for a place name (default: {GEOCODE_TIMEOUT}s)",
    )
    behaviour.add_argument("--exiftool", metavar="PATH", help="path to the exiftool binary")
    return parser


def collect_paths(paths: list[Path], recursive: bool) -> list[Path]:
    """Expand directories into image files, keeping the caller's order."""
    collected, _ = _collect(paths, recursive)
    return collected


def _collect(paths: list[Path], recursive: bool) -> tuple[list[Path], Counter[str]]:
    """The image files, and a count of the suffixes passed over.

    The second half exists because a directory scan that silently ignores a
    ``.jfif`` — which is what Chrome and Outlook save — while analysing the same
    file happily when it is named directly, looks like the file is the problem.
    """
    collected: list[Path] = []
    skipped: Counter[str] = Counter()
    for path in paths:
        if not path.is_dir():
            collected.append(path)
            continue
        found = []
        for candidate in path.rglob("*") if recursive else path.glob("*"):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() in IMAGE_SUFFIXES:
                found.append(candidate)
            elif candidate.suffix:
                skipped[candidate.suffix.lower()] += 1
        collected.extend(sorted(found))
    return collected, skipped


def _plain(message: str) -> Text:
    """A message holding a filename, so rich cannot read it as markup.

    Every one of these interpolates a name chosen by whoever made the file. As a
    bare string rich parses it: a photograph called
    ``mk[link=http:evil.example]CLICKx.jpg`` made findpic print an OSC 8
    hyperlink to an address of the file's choosing — under --no-color, where the
    help promises hyperlinks do not survive — and the name it printed was not
    the name on disk.
    """
    return Text(printable(message))


def _error_line(name: object, message: object) -> Text:
    """One file's failure, with the path printed exactly as it is.

    These were f-strings interpolated into rich markup, so a file named
    ``[bold red]OWNED[not a tag].jpg`` reported itself as ``OWNED.jpg`` — a path
    that does not exist, twice, on the one line where the true path is the whole
    point — and repainted the rest of the line. A Text is never re-parsed.
    """
    line = Text(f"{printable(name)}: ", style="red")
    line.append(printable(message))
    return line


def _col(text: str, width: int) -> str:
    """Pad or elide to ``width`` display cells, not codepoints.

    ``f"{name:<26.26}"`` counts characters. A CJK filename is two cells per
    character, so twenty-six of them are fifty-two cells and every column after
    it shifts right; an NFD-decomposed name is the opposite. The columns are the
    whole point of this mode, so they are measured in what the terminal actually
    draws.
    """
    text = printable(text)
    size = cell_len(text)
    if size <= width:
        return text + " " * (width - size)
    # Marked, because two names truncated to the same prefix printed identically
    # and neither said it had been cut.
    return set_cell_size(text, width - 1) + "…"


def _glyphs(report: Report) -> str:
    return "".join(
        LEVEL_GLYPH[report.verdicts[axis].level] for axis in AXES if axis in report.verdicts
    )


def _device(report: Report) -> str:
    device = report.device
    if device.make or device.model:
        return device.label
    return report.translator.get("ui.value.unknown_device")


def _taken(report: Report) -> tuple[str, str]:
    """The capture time and the style that says where it came from."""
    t = report.translator
    taken = (report.capture.taken or "")[:16]
    if taken:
        return taken, "grey62"
    # A recovered date rather than "no timestamp": the name of a file a
    # messenger handed back often carries the moment its tags no longer do, and
    # a directory listing that says "no timestamp" for two hundred such files is
    # answering a question findpic can already answer.
    found = timestamp_from_filename(report.file.name)
    if found is None:
        return t.get("ui.value.no_timestamp"), "grey62"
    # Parenthesised, not marked with "~": that glyph already means "fair" in the
    # verdict column three fields to the left, and the legend at the foot of the
    # listing defines it that way.
    exact = found.precision == PRECISION_SECOND
    stamp = found.moment.strftime("%Y-%m-%d %H:%M" if exact else "%Y-%m-%d")
    return f"({stamp})", "grey42"


def _place(report: Report) -> str:
    if report.location.present:
        return report.location.place or report.location.decimal or ""
    return report.translator.get("ui.value.no_location")


def summary_row(report: Report) -> str:
    """One tab-separated row, for a pipe rather than a person.

    A plain ``str`` and never a ``rich.Text``: Text's constructor strips the
    control codes rich cannot render, carriage return among them, so a file
    named ``car\rriage.jpg`` came out as ``carriage.jpg`` — a path that names
    no file, in the one field whose whole purpose is to be usable.

    The path is therefore emitted exactly as it is on disk, not through
    :func:`printable`, because this mode exists to be piped into
    ``cut -f2 | xargs``. Every other column is a value out of the photograph and
    is still sanitised. It is the same bargain ``ls`` and ``find`` make when
    their output is not a terminal, and it is why the aligned mode — the one a
    person ever sees — sanitises all five.
    """
    # Two characters, and only two, are escaped in the path: a newline ends the
    # record and a NUL ends the field for anything reading C strings, so leaving
    # them raw would break the "one file, one line" promise this mode exists to
    # keep. Escaped rather than dropped, so the row still says the name was not
    # what it appears. Everything else — control codes, bidi, invalid UTF-8 —
    # goes out untouched, because a path with characters removed names no file.
    path = report.file.path.replace("\\", "\\\\").replace("\n", "\\n").replace("\0", "\\0")
    return "\t".join(
        (
            printable(_glyphs(report)),
            path,
            printable(_device(report)),
            printable(_taken(report)[0]),
            printable(_place(report)),
        )
    )


def summary_line(report: Report, width: int = 80) -> Text:
    """One dense line per file, for scanning a directory.

    One file, one line — always. A wrapped summary is unreadable and breaks
    anything piping this into awk or grep, so every field is measured in display
    cells and every value is stripped of the newlines and escapes that would
    otherwise forge extra rows. See :func:`summary_row` for the piped form.
    """
    taken, taken_style = _taken(report)
    line = Text()
    for axis in AXES:
        verdict = report.verdicts.get(axis)
        if verdict is not None:
            line.append(LEVEL_GLYPH[verdict.level], style=LEVEL_STYLE[verdict.level])
    line.append("  ")
    line.append(_col(report.file.name, 26) + " ", style="bold white")
    line.append(_col(_device(report), 20) + " ", style="cyan")
    line.append(_col(taken, 18) + " ", style=taken_style)
    # Measured like every other column rather than left to the console's crop:
    # cropping shortened "48.858400, 2.294500" to "48.858400, 2.2", a
    # well-formed coordinate fifty kilometres from the truth, with nothing to
    # say it had been cut.
    room = max(12, width - 5 - 26 - 20 - 18 - 3)
    line.append(_col(_place(report), room), style="yellow" if report.location.present else "grey42")
    return line


def worst_level(report: Report) -> VerdictLevel:
    levels = [v.level for v in report.verdicts.values()]
    return max(levels, key=lambda level: level.rank) if levels else VerdictLevel.UNKNOWN


def _destination(out: Path | None, source: Path, marker: str) -> Path | None:
    """Where --out sends this file, or None to keep the default name.

    A directory takes every target, each keeping its own name, so a whole folder
    can be cleaned into one place without the second file colliding with the
    first. A path that is not a directory is one file, and the caller has
    already refused that when there is more than one target.
    """
    if out is None:
        return None
    if out.is_dir():
        return out / f"{source.stem}{marker}{source.suffix}"
    return out


def run_metadata_write(
    args: argparse.Namespace,
    targets: list[Path],
    console: Console,
    errors: Console,
    exiftool: ExifTool,
    translator: Translator,
) -> int:
    """Handle --backup and --restore.

    Kept apart from the reporting path on purpose: analysis prints and exits on
    what it found, while a write either happened or did not, and conflating the
    two exit codes would make a failed restore look like a clean photo.
    """
    failures = 0
    cleaned: list[Path] = []
    for path in targets:
        try:
            if args.clean:
                result = clean(
                    path, destination=_destination(args.out, path, ".clean"), exiftool=exiftool
                )
                console.print(
                    _plain(
                        translator.get(
                            "cli.clean.written",
                            target=result.written.name,
                            removed=result.removed,
                            before=result.tags_before,
                        )
                    )
                )
                cleaned.append(path)
            elif args.backup:
                written = backup(path, exiftool=exiftool)
                console.print(
                    _plain(
                        translator.get(
                            "cli.backup.written",
                            sidecar=written.name,
                            size=translator.bytes(written.stat().st_size),
                        )
                    )
                )
            else:
                result = restore(
                    args.restore,
                    path,
                    destination=_destination(args.out, path, ".restored"),
                    exiftool=exiftool,
                    force=args.force,
                )
                key = "cli.restore.written" if result.recovered else "cli.restore.nothing"
                console.print(
                    _plain(
                        translator.get(
                            key,
                            result.recovered,
                            target=result.written.name,
                            count=result.recovered,
                        )
                    )
                )
        except (RestoreError, ExifToolError, OSError) as exc:
            errors.print(_error_line(path.name, exc), no_wrap=True, crop=False, overflow="ignore")
            failures += 1
    # Once, at the end. Said after every file it became the thing the reader
    # scrolls past, which is the opposite of what it is for.
    if cleaned:
        console.print(_plain(translator.get("cli.clean.keep_backup", file=cleaned[0].name)))
    return EXIT_ERROR if failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.paths:
        parser.print_help()
        return EXIT_ERROR

    if args.width is not None and args.width < MIN_WIDTH:
        # Silently produced zero rows for every file and still exited 1, which
        # is indistinguishable from a run that found something.
        print(f"--width must be at least {MIN_WIDTH} columns.", file=sys.stderr)
        return EXIT_ERROR

    if sum(map(bool, (args.backup, args.restore, args.clean))) > 1:
        print("--backup, --restore and --clean do different things; run them one at a time.")
        return EXIT_ERROR

    no_color = args.no_color or bool(os.environ.get("NO_COLOR"))
    # Capped. Without a width, a 200-column terminal gets a 200-cell box around
    # a 24-character title and findings prose 194 characters wide — which is the
    # part a person actually reads, and unreadable at that measure.
    # (`force_terminal=None if no_color else None` used to sit here: both
    # branches were None, an unfinished edit that looked deliberate.)
    console = Console(
        no_color=no_color,
        width=args.width or min(Console().width, MAX_WIDTH),
        highlight=False,
        soft_wrap=False,
    )
    errors = Console(stderr=True, no_color=no_color, highlight=False)
    # --no-color implies never: the help promises it disables styling, and an
    # OSC 8 hyperlink is styling. TERM=dumb means a terminal that cannot.
    links = {"always": True, "never": False}.get(
        args.links,
        console.is_terminal and not no_color and os.environ.get("TERM") != "dumb",
    )

    if not ExifTool.available(args.exiftool):
        errors.print(
            f"[bold red]{Translator(args.lang or detect_language()).get('cli.error.no_exiftool')}[/]\n"
            "  sudo apt install libimage-exiftool-perl    (Debian/Kali/Ubuntu)\n"
            "  brew install exiftool                      (macOS)"
        )
        return EXIT_ERROR

    language = args.lang or detect_language()
    translator = Translator(language)
    exiftool = ExifTool(binary=args.exiftool, timeout=args.timeout)
    geocoder = Geocoder(
        enabled=not args.no_geocode,
        language=language,
        timeout=args.geocode_timeout or GEOCODE_TIMEOUT,
    )
    options = AnalysisOptions(
        geocode=not args.no_geocode,
        language=language,
        hash_file=not args.no_hash,
    )

    targets, skipped = _collect(args.paths, args.recursive)
    if not targets:
        # Pointing findpic at a photo library organised in folders — the normal
        # shape of one — said "No image files found" and exited 2 with three
        # hundred images sitting right there. It is the most likely first
        # command a new user types.
        for path in args.paths:
            if not path.is_dir():
                continue
            deeper = sum(
                1 for p in path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            )
            if deeper:
                errors.print(
                    _plain(
                        translator.get(
                            "cli.error.no_images_here", deeper, path=str(path), count=deeper
                        )
                    ),
                    style="yellow",
                )
                return EXIT_ERROR
        errors.print(_plain(translator.get("cli.error.no_images")), style="yellow")
        return EXIT_ERROR

    if skipped:
        total = sum(skipped.values())
        errors.print(
            _plain(
                translator.get(
                    "cli.warn.skipped",
                    total,
                    count=total,
                    suffixes=", ".join(sorted(skipped)),
                )
            ),
            style="grey54",
        )

    if args.backup or args.restore or args.clean:
        if args.out and len(targets) > 1 and not args.out.is_dir():
            errors.print(
                f"[red]--out {args.out} names one file but {len(targets)} were given; "
                "point it at a directory instead.[/]"
            )
            return EXIT_ERROR
        return run_metadata_write(args, targets, console, errors, exiftool, translator)

    reports: list[Report] = []
    problems: list[dict[str, object]] = []
    failures = 0
    done = 0

    for index, path in enumerate(targets):
        try:
            report = analyze(
                path,
                exiftool=exiftool,
                geocoder=geocoder,
                options=options,
                translator=translator,
            )
        except ExifToolMissing as exc:
            errors.print(Text(printable(exc), style="bold red"))
            return EXIT_ERROR
        except KeyboardInterrupt:
            # A 300-file scan takes the better part of a minute, so this happens
            # often, and it ended in a raw traceback from wherever the
            # interpreter happened to be — which reads as a crash. Anything
            # already geocoded is safe: the cache is written per photograph.
            geocoder.save_cache()
            errors.print(
                _plain(translator.get("cli.interrupted", done=done, total=len(targets))),
                style="yellow",
            )
            return EXIT_INTERRUPTED
        except ExifToolError as exc:
            errors.print(_error_line(path, exc), no_wrap=True, crop=False, overflow="ignore")
            problems.append({"file": {"path": str(path)}, "error": printable(exc)})
            failures += 1
            continue
        except OSError as exc:
            errors.print(_error_line(path, exc), no_wrap=True, crop=False, overflow="ignore")
            problems.append({"file": {"path": str(path)}, "error": printable(exc)})
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001
            # A directory run is exactly the case where one bad file must not
            # take the other nine hundred with it. A malformed geocoder payload
            # replayed out of the cache used to abort the whole run with an
            # empty stdout.
            errors.print(_error_line(path, exc), no_wrap=True, crop=False, overflow="ignore")
            problems.append({"file": {"path": str(path)}, "error": printable(exc)})
            failures += 1
            continue

        done += 1
        reports.append(report)
        if args.ndjson:
            # Written as each file finishes rather than buffered: --json has to
            # close a valid array, this does not, so it is the form that works
            # on a live stream.
            print(json.dumps(report.to_dict(include_raw=args.raw), default=str), flush=True)
            continue
        if args.json or args.summary:
            continue
        if index:
            console.print()
        render_report(
            console,
            report,
            show_info=not args.quiet,
            show_notes=args.notes,
            links=links,
        )

    geocoder.save_cache()
    # Said once, at the end, rather than as a per-file "not resolved" that never
    # explains why every later photograph is missing its place name too.
    if geocoder.gave_up:
        errors.print(
            _plain(translator.get("cli.geocode.backed_off", reason=geocoder.gave_up)),
            style="yellow",
        )

    if args.ndjson:
        for problem in problems:
            print(json.dumps(problem, default=str), flush=True)
    elif args.json:
        # Always a list. It used to be a bare object when exactly one file
        # *succeeded*, so `findpic *.jpg --json | jq '.[].file.name'` worked all
        # week and broke the morning the glob matched one file — and a file that
        # failed appeared nowhere in the payload, so a consumer could not tell
        # "no GPS" from "never read".
        payload = [r.to_dict(include_raw=args.raw) for r in reports] + problems
        print(json.dumps(payload, indent=2, default=str))
    elif args.summary:
        for report in reports:
            # One file, one line — always. A wrapped summary is unreadable and
            # breaks anything piping this into awk or grep.
            # Cropped for eyes, never for a pipe: truncating a tab-separated
            # row at the console width would cut the last field off whatever is
            # reading it, and the width of a pipe is not a real constraint.
            if sys.stdout.isatty():
                console.print(summary_line(report, width=console.width), no_wrap=True, crop=True)
            else:
                # isatty(), not console.is_terminal: rich reports a terminal
                # whenever FORCE_COLOR or TTY_COMPATIBLE is set, which is the
                # normal state on CI runners — so `--summary | awk -F'\t'` got
                # coloured fixed-width basenames and not one tab.
                #
                # Written as bytes, not printed: the path may hold bytes that
                # are not valid UTF-8, and it goes out exactly as it is on disk
                # so that `cut -f2 | xargs` names the real file.
                sys.stdout.buffer.write(os.fsencode(summary_row(report)) + b"\n")
                sys.stdout.buffer.flush()
        if reports:
            # Three glyph columns are unreadable without a key. The key belongs
            # on stderr, though: on stdout it would land in whatever is grepping
            # this, which is the reason --summary exists at all.
            errors.print(
                Text(f"^^^  {translator.get('cli.legend')}", style="grey42"),
                no_wrap=True,
                crop=False,
                overflow="ignore",
            )

    if failures:
        return EXIT_ERROR
    if any(
        worst_level(r).rank >= VerdictLevel.POOR.rank
        or any(f.severity is Severity.CRITICAL for f in r.findings)
        for r in reports
    ):
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
