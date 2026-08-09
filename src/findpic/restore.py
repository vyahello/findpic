"""Back metadata up so it can be put back, and put it back.

Everything else in findpic reads. This writes, and it exists because of an
awkward discovery: the tag dump findpic already offers is not a backup, and it
looks exactly like one. It is a column of human-readable values with the binary
tags elided to the words "(Binary data 1194 bytes, use -b option to extract)".
Hand it to exiftool and every line is read as a filename. Somebody who stripped
a photo believing they had a copy of its metadata has lost it.

So there are two operations here, and they are the answer to "can stripped
metadata be restored":

**Before.** ``backup()`` writes a MIE sidecar — exiftool's own container format,
built for exactly this. Twenty kilobytes holds everything, binary MakerNotes and
all.

**After.** ``restore()`` copies metadata from a donor into a *new* file. The
donor can be the sidecar, or the untouched original if you still have one.

The measured ceiling, on a real iPhone photo: 165 tags of 166. What does not
come back is Apple's ``AROT`` HDR block, an APP10 segment exiftool can read and
cannot write. Nothing else was lost, and every value compared byte for byte.

The one thing this cannot do is invent. If the metadata was destroyed and no
copy exists anywhere, no sidecar written after the fact will bring it back, and
nothing in this module pretends otherwise.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .exif import ExifTool, ExifToolError, ExifToolTimeout

#: exiftool's own metadata container. Chosen over XMP because XMP silently drops
#: MakerNotes and anything binary, which on a phone photo is most of what makes
#: the file identifiable in the first place.
SIDECAR_SUFFIX = ".mie"

#: Copy every tag, then the ICC profile by name.
#:
#: The second part is not redundant. ``-all:all`` moves tags; a colour profile is
#: not a tag but a block, and it comes back only when named with the copy
#: operator. Without it the round trip silently loses 28 entries and the picture
#: displays with the wrong colours — which is how this was found.
COPY_ARGS = ("-all:all", "-icc_profile<icc_profile", "-unknown")


class RestoreError(ExifToolError):
    """A backup or restore could not be completed."""


@dataclass(frozen=True)
class RestoreResult:
    """What a write actually achieved, in numbers the caller can print."""

    source: Path
    target: Path
    written: Path
    tags_before: int
    tags_after: int

    @property
    def recovered(self) -> int:
        return max(0, self.tags_after - self.tags_before)


def _run(exiftool: ExifTool, argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=exiftool.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExifToolTimeout("exiftool timed out writing metadata") from exc
    except OSError as exc:
        raise RestoreError(f"could not run exiftool: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RestoreError(detail[0] if detail else "exiftool reported an error")
    return result.stdout


def _refuse_to_clobber(destination: Path) -> None:
    """Never write over something that is already there.

    A restore is run by someone who has already lost data once. Overwriting on
    the second attempt is not a risk worth taking to save them typing a name.
    """
    if destination.exists():
        raise RestoreError(f"{destination} already exists — choose another name")


def sidecar_path(path: Path) -> Path:
    """Where a file's backup lives: alongside it, with the suffix appended.

    ``photo.jpg`` becomes ``photo.jpg.mie`` rather than ``photo.mie`` so that
    ``photo.jpg`` and ``photo.png`` in one folder do not share a backup.
    """
    return path.with_name(path.name + SIDECAR_SUFFIX)


def backup(
    source: str | os.PathLike[str],
    destination: Path | None = None,
    *,
    exiftool: ExifTool | None = None,
) -> Path:
    """Write every piece of metadata to a sidecar that can restore it.

    The source is opened read-only and never modified.
    """
    tool = exiftool or ExifTool()
    origin = tool._checked_path(source)  # noqa: SLF001 - same package, same checks
    target = destination or sidecar_path(origin)
    _refuse_to_clobber(target)
    _run(tool, [tool.binary, "-o", str(target), *COPY_ARGS, str(origin)])
    if not target.exists():
        raise RestoreError("exiftool reported success but wrote no sidecar")
    return target


def restore(
    donor: str | os.PathLike[str],
    target: str | os.PathLike[str],
    destination: Path | None = None,
    *,
    exiftool: ExifTool | None = None,
) -> RestoreResult:
    """Copy metadata from ``donor`` onto ``target``, into a new file.

    ``donor`` may be a sidecar written by :func:`backup` or any image that still
    has its metadata — the untouched original, or another shot from the same
    camera if all you want is the device.

    The stripped file is left exactly as it was. It is evidence of what a
    pipeline did to it, and a restore is a judgement call; the two should not be
    the same file.
    """
    tool = exiftool or ExifTool()
    source = tool._checked_path(donor)  # noqa: SLF001
    stripped = tool._checked_path(target)  # noqa: SLF001
    if source == stripped:
        raise RestoreError("the donor and the file to restore are the same file")

    written = destination or stripped.with_name(f"{stripped.stem}.restored{stripped.suffix}")
    _refuse_to_clobber(written)

    before = tool.read(stripped).tag_count
    _run(
        tool,
        [tool.binary, "-tagsfromfile", str(source), *COPY_ARGS, "-o", str(written), str(stripped)],
    )
    if not written.exists():
        raise RestoreError("exiftool reported success but wrote no file")
    after = tool.read(written).tag_count
    return RestoreResult(
        source=source,
        target=stripped,
        written=written,
        tags_before=before,
        tags_after=after,
    )
