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
from .jpegprint import fingerprint

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

#: Written into every restored file, after the donor's tags, so it cannot be
#: overwritten by them.
#:
#: A restored file is a file whose metadata came from somewhere else. Left
#: unmarked it asserts a camera, a moment and a place with exactly the
#: confidence of a photograph that recorded them itself, and nothing downstream
#: — including findpic — can tell the difference. I checked: before this, a
#: restore across two unrelated photos produced a file findpic graded LIKELY
#: ORIGINAL and whose fabricated coordinates it printed as the location.
#:
#: XMP's media-management history is the right home for it. It is the standard
#: field for "this file has a processing lineage", every serious tool reads it,
#: and findpic's own xmp_edit_history rule already does — so marking the file
#: makes the existing analysis notice, without a special case.
PROVENANCE_ARGS = (
    "-XMP-xmpMM:HistoryAction=metadata_restored",
    "-XMP-xmpMM:HistorySoftwareAgent=findpic",
    "-XMP-xmpMM:HistoryWhen=now",
)


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


def _same_frame(donor: Path, target: Path) -> bool | None:
    """Whether two files hold pictures of the same pixel dimensions.

    ``None`` when either is not a JPEG we can read a frame header out of, in
    which case the caller has no evidence either way and should say so rather
    than assume.

    This is not a test of whether the two are the same photograph — nothing
    short of looking at them is — but differing dimensions do mean the donor's
    record of its own size, lens and subject distance no longer describes the
    target. That is the cheap half of the question, and it is the half that
    catches somebody pointing at the wrong file.
    """
    first, second = fingerprint(donor), fingerprint(target)
    if not (first.ok and second.ok):
        return None
    if not (first.width and second.width):
        return None
    return (first.width, first.height) == (second.width, second.height)


def restore(
    donor: str | os.PathLike[str],
    target: str | os.PathLike[str],
    destination: Path | None = None,
    *,
    exiftool: ExifTool | None = None,
    force: bool = False,
) -> RestoreResult:
    """Copy metadata from ``donor`` onto ``target``, into a new file.

    ``donor`` may be a sidecar written by :func:`backup` or any image that still
    has its metadata — the untouched original, or another shot from the same
    camera if all you want is the device.

    The stripped file is left exactly as it was. It is evidence of what a
    pipeline did to it, and a restore is a judgement call; the two should not be
    the same file.

    Two safeguards, both learned from watching this function forge a photograph.
    A donor whose picture is a different size is refused unless ``force`` is
    set, because its dimensions, lens and subject distance describe something
    else. And the result is always marked as having been restored, so it cannot
    pass itself off as a file that recorded its own metadata.
    """
    tool = exiftool or ExifTool()
    source = tool._checked_path(donor)  # noqa: SLF001
    stripped = tool._checked_path(target)  # noqa: SLF001
    if source == stripped:
        raise RestoreError("the donor and the file to restore are the same file")

    if not force and _same_frame(source, stripped) is False:
        raise RestoreError(
            f"{source.name} and {stripped.name} hold pictures of different sizes, so the "
            "metadata in one does not describe the other. Pass force=True (--force) if "
            "you know they are the same photograph at a different size."
        )

    written = destination or stripped.with_name(f"{stripped.stem}.restored{stripped.suffix}")
    _refuse_to_clobber(written)

    before = tool.read(stripped).tag_count
    _run(
        tool,
        [
            tool.binary,
            "-tagsfromfile",
            str(source),
            *COPY_ARGS,
            # After the copy, so the donor's own history cannot overwrite it.
            *PROVENANCE_ARGS,
            f"-XMP-xmpMM:HistoryParameters=metadata restored from {source.name} by findpic",
            "-o",
            str(written),
            str(stripped),
        ],
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
