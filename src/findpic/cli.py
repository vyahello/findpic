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
from .render.terminal import LEVEL_GLYPH, LEVEL_STYLE, render_report

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
    taken = (report.capture.taken or "")[:16] or t.get("ui.value.no_timestamp")
    line.append(f"{taken:<17} ", style="grey62")
    if report.location.present:
        line.append(report.location.place or report.location.decimal or "", style="yellow")
    else:
        line.append(t.get("ui.value.no_location"), style="grey42")
    return line


def worst_level(report: Report) -> VerdictLevel:
    levels = [v.level for v in report.verdicts.values()]
    return max(levels, key=lambda level: level.rank) if levels else VerdictLevel.UNKNOWN


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.paths:
        parser.print_help()
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
