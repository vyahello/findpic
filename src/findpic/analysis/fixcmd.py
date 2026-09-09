"""Build the exiftool command findpic prints beneath a finding.

Every command in the report used to be a hand-written constant, and each one was
wrong in the same three ways: it named ``photo.jpg`` instead of the file the
reader is looking at, it wrote ``clean_copy.jpg`` regardless of what the input
actually was, and one of them interpolated the filename into a double-quoted
shell string. A file named ``IMG_1234"; id #.jpg`` made findpic print a line
that runs ``id`` when pasted.

So nothing here builds a shell string by interpolation. Arguments go through
:func:`shlex.quote`, which emits POSIX single-quoting — byte-identical in sh,
bash and zsh — and the result round-trips through :func:`shlex.split` back to
the exact original path. That property is what the tests assert: a remediation
is a *parseable argv*, not a string that looks like one.

The other half is the flag itself. The privacy rules find a tag by asking
:class:`~findpic.exif.Metadata` for a name like ``IPTC:Source``, and Metadata
falls back from a group-qualified miss to a bare-name match — so that request
also answers for ``XMP-dc:Source``, ``XMP-photoshop:Source`` and every other
group exiftool files ``Source`` under. A fix naming ``-iptc:all=`` therefore
misses the value the finding just reported, and no list of per-group flags can
ever be complete. :func:`flag_for` drops the group instead, which makes the
command's reach exactly congruent with the rule's: whatever the scan could see,
the fix can delete.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from pathlib import Path

#: Tags that exiftool will not delete one at a time, mapped to the flag that
#: does reach them. Nothing is in here yet — Apple's identifiers, which look
#: like the obvious candidates, delete individually and leave the vendor block
#: standing, which is far better than taking the block with them.
GROUP_ONLY: dict[str, str] = {}


def flag_for(tag: str) -> str:
    """The exiftool delete flag for one scanned tag, without its group.

    ``IPTC:Source`` becomes ``-Source=``, which removes the value wherever it
    is — and wherever it is, is where the rule found it.
    """
    if tag in GROUP_ONLY:
        return GROUP_ONLY[tag]
    return f"-{tag.rsplit(':', 1)[-1]}="


def flags_for(tags: Iterable[str]) -> tuple[str, ...]:
    """Delete flags for several tags, in order, without repeats."""
    return tuple(dict.fromkeys(flag_for(tag) for tag in tags))


def output_name(path: str, extension: str | None, marker: str = "clean") -> str:
    """``photo.heic`` -> ``photo.clean.heic``, beside the original.

    The suffix comes from the input. Every hand-written command hard-coded
    ``.jpg``, and exiftool refuses to change a file's type on the way out —
    ``Error: Can't create JPEG files from other types`` — so all eleven printed
    fixes failed on a HEIC, exit 1, nothing written.

    The stem comes from the input too, so ``findpic album/ --recursive`` prints
    two hundred different output names rather than two hundred commands racing
    for one ``clean_copy.jpg``.
    """
    source = Path(path)
    suffix = extension or source.suffix.lstrip(".") or "jpg"
    return str(source.with_name(f"{source.stem}.{marker}.{suffix}"))


#: Characters that must not appear literally in a printed command: a control
#: code would reach the terminal, and a newline would split one command into two.
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def shell_quote(value: str, *, is_path: bool = False) -> str:
    """Quote one argument so the command both runs and is safe to print.

    ``shlex.quote`` is correct for the shell and wrong for the screen: it wraps
    a filename holding a newline or an ESC in single quotes and passes the byte
    through, so the printed command carries a control character straight to the
    terminal. Sanitising the finished command instead — which is what this
    replaced — rewrites the path *inside* the quotes, and the command then names
    a file that does not exist, or, worse, a different file that does.

    So a value that cannot be printed literally is emitted in ANSI-C quoting,
    ``$'...'``, which bash and zsh both understand and which contains no control
    characters at all. A leading dash is prefixed with ``./`` because exiftool
    reads ``-rf.jpg`` as an option however the shell quotes it.
    """
    # Only a path: exiftool's own flags start with a dash and must keep it.
    if is_path and value.startswith("-"):
        value = f"./{value}"
    if not _UNPRINTABLE.search(value):
        return shlex.quote(value)
    escaped = value.encode("unicode_escape").decode("ascii").replace("'", "\\'")
    return f"$'{escaped}'"


def command(
    path: str,
    extension: str | None,
    args: Sequence[str],
    *,
    marker: str = "clean",
) -> str:
    """One ready-to-paste exiftool command that writes a new file."""
    target = output_name(path, extension, marker)
    return " ".join(
        (
            "exiftool",
            *(shell_quote(arg) for arg in args),
            "-o",
            shell_quote(target, is_path=True),
            shell_quote(path, is_path=True),
        )
    )
