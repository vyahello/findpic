"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from . import __version__
from .analysis import AnalysisOptions, analyze
from .exif import ExifTool, ExifToolError, ExifToolMissing
from .geocode import Geocoder
from .i18n import LANGUAGE_NAMES, Translator, available_languages, detect_language
from .models import Report, Severity, VerdictLevel
from .recover import PRECISION_SECOND, timestamp_from_filename
from .render.terminal import LEVEL_GLYPH, LEVEL_STYLE, render_report
from .restore import RestoreError, backup, clean, restore

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
}

#: Shell exit codes, so findpic composes with scripts and CI.
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


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
    output.add_argument("--summary", "-s", action="store_true", help="one line per file")
    output.add_argument("--quiet", "-q", action="store_true", help="hide informational findings")
    output.add_argument("--notes", action="store_true", help="show exiftool's own warnings")
    output.add_argument("--no-color", action="store_true", help="disable colour and styling")

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
    behaviour.add_argument("--exiftool", metavar="PATH", help="path to the exiftool binary")
    return parser


def collect_paths(paths: list[Path], recursive: bool) -> list[Path]:
    """Expand directories into image files, keeping the caller's order."""
    collected: list[Path] = []
    for path in paths:
        if path.is_dir():
            walker = path.rglob("*") if recursive else path.glob("*")
            collected.extend(
                sorted(p for p in walker if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
            )
        else:
            collected.append(path)
    return collected


def summary_line(report: Report) -> Text:
    """One dense line per file, for scanning a directory.

    Columns are fixed-width so the eye can run down them. The timestamp is cut to
    the minute — enough to place a photo, short enough to leave room for where.
    """
    line = Text()
    for axis in ("originality", "privacy", "structure"):
        verdict = report.verdicts.get(axis)
        if verdict is None:
            continue
        line.append(LEVEL_GLYPH[verdict.level], style=LEVEL_STYLE[verdict.level])
    line.append("  ")
    line.append(f"{report.file.name:<26.26} ", style="bold white")
    t = report.translator
    device = (
        report.device.label
        if (report.device.make or report.device.model)
        else t.get("ui.value.unknown_device")
    )
    line.append(f"{device:<20.20} ", style="cyan")
    # A recovered date rather than "no timestamp": the name of a file a
    # messenger handed back often carries the moment its tags no longer do, and
    # a directory listing that says "no timestamp" for two hundred such files is
    # answering a question findpic can already answer. Marked with a tilde and
    # dimmer, because it came from the name.
    taken, taken_style = (report.capture.taken or "")[:16], "grey62"
    if not taken:
        found = timestamp_from_filename(report.file.name)
        if found is not None:
            exact = found.precision == PRECISION_SECOND
            # Parenthesised, not marked with "~": that glyph already means
            # "fair" in the verdict column three fields to the left, and the
            # legend at the foot of the listing defines it that way.
            stamp = found.moment.strftime("%Y-%m-%d %H:%M" if exact else "%Y-%m-%d")
            taken = f"({stamp})"
            taken_style = "grey42"
        else:
            taken = t.get("ui.value.no_timestamp")
    line.append(f"{taken:<18} ", style=taken_style)
    if report.location.present:
        line.append(report.location.place or report.location.decimal or "", style="yellow")
    else:
        line.append(t.get("ui.value.no_location"), style="grey42")
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
                    translator.get(
                        "cli.clean.written",
                        target=result.written.name,
                        removed=result.removed,
                        before=result.tags_before,
                    )
                )
                cleaned.append(path)
            elif args.backup:
                written = backup(path, exiftool=exiftool)
                console.print(
                    translator.get(
                        "cli.backup.written",
                        sidecar=written.name,
                        size=translator.bytes(written.stat().st_size),
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
                    translator.get(
                        key,
                        result.recovered,
                        target=result.written.name,
                        count=result.recovered,
                    )
                )
        except (RestoreError, ExifToolError, OSError) as exc:
            errors.print(f"[red]{path.name}:[/] {exc}")
            failures += 1
    # Once, at the end. Said after every file it became the thing the reader
    # scrolls past, which is the opposite of what it is for.
    if cleaned:
        console.print(translator.get("cli.clean.keep_backup", file=cleaned[0].name))
    return EXIT_ERROR if failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.paths:
        parser.print_help()
        return EXIT_ERROR

    if sum(map(bool, (args.backup, args.restore, args.clean))) > 1:
        print("--backup, --restore and --clean do different things; run them one at a time.")
        return EXIT_ERROR

    no_color = args.no_color or bool(os.environ.get("NO_COLOR"))
    console = Console(
        no_color=no_color,
        force_terminal=None if no_color else None,
        highlight=False,
        soft_wrap=False,
    )
    errors = Console(stderr=True, no_color=no_color, highlight=False)

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
    geocoder = Geocoder(enabled=not args.no_geocode, language=language)
    options = AnalysisOptions(
        geocode=not args.no_geocode,
        language=language,
        hash_file=not args.no_hash,
    )

    targets = collect_paths(args.paths, args.recursive)
    if not targets:
        errors.print(f"[yellow]{translator.get('cli.error.no_images')}[/]")
        return EXIT_ERROR

    if args.backup or args.restore or args.clean:
        if args.out and len(targets) > 1 and not args.out.is_dir():
            errors.print(
                f"[red]--out {args.out} names one file but {len(targets)} were given; "
                "point it at a directory instead.[/]"
            )
            return EXIT_ERROR
        return run_metadata_write(args, targets, console, errors, exiftool, translator)

    reports: list[Report] = []
    failures = 0

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
            errors.print(f"[bold red]{exc}[/]")
            return EXIT_ERROR
        except ExifToolError as exc:
            errors.print(f"[red]{path}:[/] {exc}")
            failures += 1
            continue
        except OSError as exc:
            errors.print(f"[red]{path}:[/] {exc}")
            failures += 1
            continue

        reports.append(report)
        if args.json or args.summary:
            continue
        if index:
            console.print()
        render_report(console, report, show_info=not args.quiet, show_notes=args.notes)

    geocoder.save_cache()

    if args.json:
        payload = [r.to_dict(include_raw=args.raw) for r in reports]
        print(json.dumps(payload if len(payload) != 1 else payload[0], indent=2, default=str))
    elif args.summary:
        for report in reports:
            # One file, one line — always. A wrapped summary is unreadable and
            # breaks anything piping this into awk or grep.
            console.print(summary_line(report), no_wrap=True, crop=True)
        if reports:
            # Three glyph columns are unreadable without a key. The key belongs
            # on stderr, though: on stdout it would land in whatever is grepping
            # this, which is the reason --summary exists at all.
            errors.print(Text(f"^^^  {translator.get('cli.legend')}", style="grey42"))

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
