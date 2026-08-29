"""The work behind the handlers: fetch, analyse, clean, and clean up.

Two things make this more than a thin wrapper.

**exiftool is a blocking subprocess** and geocoding is a blocking HTTP call, so
every analysis runs in a worker thread. Doing it inline would stall the event
loop for every other user for the duration.

**Files must not accumulate.** A local Bot API server writes each upload to disk
and never removes it, so the bot deletes the source once it is done — guarded so
it can only ever delete inside its own token directory, never a sibling bot's
files on the same shared volume.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot

from ..analysis import AnalysisOptions, analyze
from ..exif import ExifTool, Metadata
from ..geocode import Geocoder
from ..models import Report
from ..restore import SIDECAR_SUFFIX, backup
from .archive import Archive, Stored
from .config import Config
from .filenames import clean_copy_name, display_name, safe_suffix

logger = logging.getLogger(__name__)

#: Strip identity but keep the two tags that affect how the picture *displays*.
#: A bare `-all=` also drops Orientation, which silently rotates photos sideways
#: in every viewer, and the ICC profile, which shifts the colours.
CLEAN_ARGS = (
    "-all=",
    "-tagsfromfile",
    "@",
    "-Orientation",
    "-ICC_Profile",
)


#: Which tag groups carry which of the three things a reader cares about. The
#: check is deliberately on the *presence of the group*, not on a tag count: a
#: file can keep forty harmless structural tags and still have lost every one
#: that pointed at a person.
_LOSS_PROBES = (
    ("location", ("GPS:GPSLatitude", "GPSLatitude", "Composite:GPSPosition")),
    ("time", ("ExifIFD:DateTimeOriginal", "DateTimeOriginal", "IFD0:ModifyDate")),
    ("device", ("IFD0:Make", "Make", "IFD0:Model", "Model")),
    ("people", ("XMP-mwg-rs:RegionName", "RegionName", "XMP-mwg-rs:RegionType")),
)


@dataclass
class KeepRequest:
    """Ask for a copy to be kept, with the budget it has to fit inside.

    The two byte counts are read from the manifest by the caller rather than by
    the archive, because the archive is deliberately SQL-free — it knows about
    files and nothing else.

    Not frozen, and that is load-bearing: ``stored`` is filled in the moment the
    copy lands, so the caller still learns a file was kept even when the
    analysis afterwards raises. Otherwise a picture the bot could not read would
    sit on disk with no row naming it, and nothing would ever delete it.
    """

    user_id: int
    when: str
    held_bytes: int = 0
    user_bytes: int = 0
    stored: Stored | None = None


@dataclass(frozen=True)
class Analysis:
    """A report, and what became of the copy — when one was asked for."""

    report: Report
    stored: Stored | None = None


@dataclass(frozen=True)
class CleanResult:
    """A scrubbed copy and what leaving the metadata behind actually cost.

    ``removed`` is None when the count could not be taken. ``lost`` names the
    categories that were in the file and are not in the copy — the part a
    reader can act on, as against a number they cannot.
    """

    data: bytes
    name: str
    removed: int | None
    lost: tuple[str, ...]


def _discard(local: Path) -> None:
    """Remove the working copy and the directory it was alone in.

    Unlinking the file alone would leave an empty directory per request on a
    512 MB tmpfs, which is memory that only a restart reclaims.
    """
    shutil.rmtree(local.parent, ignore_errors=True)


def _what_left(before: Metadata | None, after: Metadata | None) -> tuple[str, ...]:
    """Which categories of identifying metadata the strip actually removed."""
    if before is None or after is None:
        return ()
    return tuple(name for name, tags in _LOSS_PROBES if before.has(*tags) and not after.has(*tags))


class AnalysisService:
    """Fetches files from Telegram and runs findpic against them."""

    def __init__(self, config: Config, exiftool: ExifTool, archive: Archive | None = None) -> None:
        self.config = config
        self.exiftool = exiftool
        self.archive = archive
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        # One geocoder for the life of the process, not one per photo. A fresh
        # instance re-reads the whole cache file inside the worker thread on
        # every GPS picture, and two concurrent analyses load-mutate-save it
        # last-writer-wins — so what looked like a cache was costing a live
        # Nominatim round trip, serialised behind a one-per-second lock held
        # across time.sleep, for every photograph the bot has ever seen.
        self._geocoders: dict[str, Geocoder] = {}

    def _geocoder(self, language: str) -> Geocoder:
        """The shared geocoder for a language. Place names differ per language,
        so the cache has to be keyed by one."""
        if language not in self._geocoders:
            # Deliberately in the scratch directory, which is a tmpfs that dies
            # with the container — not on the persistent volume. The cache holds
            # Nominatim's full reply for every located photograph: a street
            # address at four decimal places, keyed by coordinate. Nothing
            # purges it, no retention clock reaches it, and it exists even with
            # ANALYTICS=0, whose notice says "never recorded: where and when a
            # photo was taken". A cache that outlives the answer is a record.
            self._geocoders[language] = Geocoder(
                enabled=True,
                language=language,
                cache_file=self.config.work_dir / f"geocode-{language}.json",
            )
        return self._geocoders[language]

    # ------------------------------------------------------------------ fetch

    async def _fetch(self, bot: Bot, file_id: str, file_name: str) -> tuple[Path, Path | None]:
        """Materialise the file locally.

        Returns ``(local_copy, server_path)``. ``server_path`` is set only when a
        local Bot API server wrote the file to the shared volume, in which case
        it is ours to delete afterwards.
        """
        file = await bot.get_file(file_id)
        if not file.file_path:
            raise RuntimeError("Telegram returned no file path")

        # Never the sender's suffix verbatim: it reaches open() as a real path,
        # and a NUL byte in it raises ValueError from inside the C layer — not
        # an OSError, so it escapes every handler written to expect one.
        suffix = safe_suffix(file_name, "") or safe_suffix(file.file_path, ".bin")
        # A directory per request, not a filename per request. The analysis
        # renames this file to the name its *sender* chose, because filename
        # findings would otherwise be nonsense — and two people sending
        # "photo.jpg" at the same moment then resolved to one path in a shared
        # directory. The second move overwrote the first, and the first sender
        # was shown an analysis of the second sender's photograph. With the
        # archive on, that picture would also have been filed under their id.
        room = self.config.work_dir / uuid.uuid4().hex
        room.mkdir(parents=True, exist_ok=True)
        target = room / f"upload{suffix}"

        # From here on the file may exist on disk, so every exit has to remove
        # it. work_dir is a RAM-backed tmpfs in production: a partial copy left
        # behind by a failed fetch is not a stray file, it is leaked memory that
        # only a restart reclaims.
        try:
            server_path: Path | None = None
            if self.config.api_is_local:
                candidate = Path(file.file_path)
                if candidate.is_absolute():
                    try:
                        # Copy rather than analyse in place: exiftool must never
                        # be pointed at a directory another service is writing.
                        await asyncio.to_thread(shutil.copyfile, candidate, target)
                        return target, candidate
                    except FileNotFoundError:
                        logger.warning("local API reported a path we cannot see: %s", candidate)
                    except PermissionError:
                        # Almost always a UID mismatch against the volume's ACL.
                        # Say so once, loudly, instead of failing every photo in
                        # silence.
                        logger.error(
                            "cannot read %s — the container runs as uid %s, which the "
                            "shared volume's ACL does not grant access to. Set APP_UID "
                            "in deploy/.env to the host user that does, and rebuild.",
                            candidate,
                            os.getuid(),
                        )
                        raise

            # Cloud API, or a local path we could not see — download over HTTP.
            await bot.download_file(file.file_path, destination=target)
            return target, server_path
        except BaseException:
            shutil.rmtree(room, ignore_errors=True)
            raise

    def _delete_source(self, server_path: Path | None) -> None:
        """Remove the upload from the shared volume, scoped to our own bot.

        The volume is shared with other bots, so the path is resolved and checked
        against this bot's token directory before anything is unlinked.
        """
        if server_path is None or not self.config.delete_source_files:
            return
        try:
            resolved = server_path.resolve()
            allowed = self.config.token_directory.resolve()
            if not resolved.is_relative_to(allowed):
                logger.warning("refusing to delete outside our token dir: %s", resolved)
                return
            resolved.unlink(missing_ok=True)
        except OSError as error:
            logger.warning("could not delete source file: %s", error)

    # --------------------------------------------------------------- analyse

    async def analyse(
        self,
        *,
        bot: Bot,
        file_id: str,
        file_name: str,
        language: str,
        keep: KeepRequest | None = None,
    ) -> Analysis:
        """Read the file, and keep a copy of it when asked to.

        The copy is taken here rather than in the handler, and taken *before*
        the analysis runs. Here, because the working copy is unlinked in the
        ``finally`` below and the Bot API server's own copy is mounted read-only
        and deleted by the janitor five minutes later — this is the only window
        in which the bytes exist anywhere the bot can reach. Before the analysis,
        because a file that crashes exiftool is precisely the one an operator
        wants on disk to reproduce with.
        """
        local, server_path = await self._fetch(bot, file_id, file_name)
        stored: Stored | None = None
        try:
            if keep is not None and self.archive is not None:
                keep.stored = stored = await asyncio.to_thread(
                    self.archive.store,
                    local,
                    user_id=keep.user_id,
                    when=keep.when,
                    claimed_suffix=safe_suffix(file_name, ""),
                    held_bytes=keep.held_bytes,
                    user_bytes=keep.user_bytes,
                )
            # Give the report the name the user sent, not our temporary one —
            # filename-based findings would otherwise be nonsense.
            named = local.with_name(display_name(file_name))
            if named != local:
                await asyncio.to_thread(shutil.move, str(local), str(named))
                local = named

            options = AnalysisOptions(geocode=True, language=language, hash_file=True)
            report = await asyncio.to_thread(
                analyze,
                local,
                self.exiftool,
                self._geocoder(language),
                options,
            )
            return Analysis(report=report, stored=stored)
        finally:
            _discard(local)
            self._delete_source(server_path)

    # ----------------------------------------------------------------- clean

    async def clean(self, *, bot: Bot, file_id: str, file_name: str) -> CleanResult:
        """A scrubbed copy, plus an honest account of what left with the tags.

        The count alone was never enough and could be wrong in both directions:
        a failed *before* read reported "0 tags removed" on a file that had just
        been stripped, and a failed *after* read reported every tag as removed
        while dozens remained. Both were silent. So a count that could not be
        taken is now ``None`` and the caption says so, and alongside it goes the
        thing a reader can actually act on — whether the place, the time and the
        camera are gone.
        """
        local, server_path = await self._fetch(bot, file_id, file_name)
        cleaned: Path | None = None
        try:
            before = await asyncio.to_thread(self._read, local)
            cleaned = await asyncio.to_thread(self._strip, local)
            after = await asyncio.to_thread(self._read, cleaned)
            data = cleaned.read_bytes()

            removed = None
            if before is not None and after is not None:
                removed = max(0, before.tag_count - after.tag_count)
            return CleanResult(
                data=data,
                name=clean_copy_name(file_name),
                removed=removed,
                lost=_what_left(before, after),
            )
        finally:
            if cleaned is not None:
                cleaned.unlink(missing_ok=True)
            _discard(local)
            self._delete_source(server_path)

    async def backup(self, *, bot: Bot, file_id: str, file_name: str) -> tuple[bytes, str]:
        """Return ``(bytes, filename)`` for a sidecar that can restore this file.

        The counterpart to :meth:`clean`. Offering to strip a photo without
        offering to keep a copy of what is being stripped is how somebody loses
        the only record of where and when a picture was taken.
        """
        local, server_path = await self._fetch(bot, file_id, file_name)
        try:
            sidecar = await asyncio.to_thread(self._sidecar, local)
            data = sidecar.read_bytes()
            sidecar.unlink(missing_ok=True)
            return data, f"{display_name(file_name)}{SIDECAR_SUFFIX}"
        finally:
            _discard(local)
            self._delete_source(server_path)

    def _sidecar(self, source: Path) -> Path:
        """Build the sidecar in scratch space, outside the temporary directory
        that the caller is about to delete."""
        handle, name = tempfile.mkstemp(suffix=SIDECAR_SUFFIX, dir=self.config.work_dir)
        os.close(handle)
        output = Path(name)
        # backup() refuses to overwrite, and mkstemp has already created it.
        output.unlink()
        return backup(source, output, exiftool=self.exiftool)

    def _read(self, path: Path) -> Metadata | None:
        """Read a file's tags, or None when the read itself failed.

        None rather than an empty result on purpose: "the file has no metadata"
        and "I could not look" are different answers, and collapsing them is
        what let the clean-copy caption report a false count.
        """
        try:
            return self.exiftool.read(path, validate=False)
        except Exception:  # noqa: BLE001 - a caption is not worth failing over
            logger.warning("could not count tags in %s", path.name, exc_info=True)
            return None

    def _strip(self, source: Path) -> Path:
        """Run exiftool to produce a scrubbed copy. Never touches the input."""
        with tempfile.TemporaryDirectory(dir=self.config.work_dir) as scratch:
            output = Path(scratch) / f"clean{source.suffix or '.jpg'}"
            argv = [
                self.exiftool.binary,
                *CLEAN_ARGS,
                "-o",
                str(output),
                str(source.resolve()),
            ]
            result = subprocess.run(
                argv, capture_output=True, timeout=self.exiftool.timeout, check=False
            )
            if not output.exists():
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"exiftool produced no output: {stderr[:200]}")
            # Move it out of the scratch directory before that is torn down.
            final = self.config.work_dir / f"{uuid.uuid4().hex}{output.suffix}"
            shutil.move(str(output), str(final))
            return final
