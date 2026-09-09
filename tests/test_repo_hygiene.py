"""Invariants about the repository itself, not about any photograph.

These exist because the failures they catch are invisible locally and only show
up in CI, ten minutes after the push.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

#: `magick` is ImageMagick 7. Debian and Ubuntu ship version 6, whose binary is
#: `convert`, and CI installs the distro package — so a test that names `magick`
#: directly passes on this laptop and fails on every runner. `conftest.py`
#: provides the `magick` fixture and `_magick` helper, both of which try each
#: name and skip when neither is there.
_DIRECT_MAGICK = re.compile(r"""["']magick["']""")


def test_no_test_shells_out_to_magick_by_name() -> None:
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not _DIRECT_MAGICK.search(line):
                continue
            # shutil.which("magick") or shutil.which("convert") is the correct
            # form — it is the fallback itself.
            if "which(" in line:
                continue
            offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "use the `magick` fixture from conftest.py, which falls back to "
        "`convert`:\n  " + "\n  ".join(offenders)
    )


def test_the_server_address_is_not_in_the_public_repository() -> None:
    """The repository is public and the notes that hold real values are ignored.

    Checked as a test rather than trusted to review: a host, an account name or
    an ssh port pasted into a docstring is not something a diff makes obvious.
    """
    secrets = re.compile(r"178\.105\.143\.68|\bcax@|ssh\s+-p\s+2022|-P\s+2022")
    tracked = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in {".git", ".venv", "__pycache__", "samples"} for part in path.parts)
        and path.suffix in {".py", ".md", ".yml", ".yaml", ".json", ".toml", ".sh"}
        and not path.name.endswith(".local.md")
    ]
    offenders = []
    for path in tracked:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):  # pragma: no cover - binary or unreadable
            continue
        if secrets.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"real server details in tracked files: {offenders}"
