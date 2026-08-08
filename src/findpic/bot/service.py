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
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from aiogram import Bot

from ..analysis import AnalysisOptions, analyze
from ..exif import ExifTool
from ..geocode import Geocoder
from ..models import Report
from .config import Config

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


class AnalysisService:
    """Fetches files from Telegram and runs findpic against them."""

    def __init__(self, config: Config, exiftool: ExifTool) -> None:
        self.config = config
        self.exiftool = exiftool
        self.config.work_dir.mkdir(parents=True, exist_ok=True)

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

        suffix = Path(file_name).suffix or Path(file.file_path).suffix or ".bin"
        target = self.config.work_dir / f"{uuid.uuid4().hex}{suffix}"

        server_path: Path | None = None
        if self.config.api_is_local:
            candidate = Path(file.file_path)
            if candidate.is_absolute() and candidate.exists():
                # Copy rather than analyse in place: exiftool must never be
                # pointed at a directory another service is still writing to.
                await asyncio.to_thread(shutil.copyfile, candidate, target)
                server_path = candidate
                return target, server_path

        # Cloud API, or a local path we could not see — download over HTTP.
        await bot.download_file(file.file_path, destination=target)
        return target, server_path

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

    async def analyse(self, *, bot: Bot, file_id: str, file_name: str, language: str) -> Report:
        local, server_path = await self._fetch(bot, file_id, file_name)
        try:
            # Give the report the name the user sent, not our temporary one —
            # filename-based findings would otherwise be nonsense.
            named = local.with_name(Path(file_name).name)
            if named != local:
                await asyncio.to_thread(shutil.move, str(local), str(named))
                local = named

            options = AnalysisOptions(geocode=True, language=language, hash_file=True)
            return await asyncio.to_thread(
                analyze,
                local,
                self.exiftool,
                Geocoder(enabled=True, language=language),
                options,
            )
        finally:
            local.unlink(missing_ok=True)
            self._delete_source(server_path)

    # ----------------------------------------------------------------- clean

    async def clean(self, *, bot: Bot, file_id: str, file_name: str) -> tuple[bytes, str, int]:
        """Return ``(bytes, filename, tags_removed)`` for a scrubbed copy."""
        local, server_path = await self._fetch(bot, file_id, file_name)
        try:
            before = await asyncio.to_thread(self._count_tags, local)
            cleaned = await asyncio.to_thread(self._strip, local)
            after = await asyncio.to_thread(self._count_tags, cleaned)
            data = cleaned.read_bytes()
            cleaned.unlink(missing_ok=True)

            stem = Path(file_name).stem or "photo"
            suffix = Path(file_name).suffix or ".jpg"
            return data, f"{stem}_clean{suffix}", max(0, before - after)
        finally:
            local.unlink(missing_ok=True)
            self._delete_source(server_path)

    def _count_tags(self, path: Path) -> int:
        try:
            return self.exiftool.read(path, validate=False).tag_count
        except Exception:  # noqa: BLE001 - a count is not worth failing over
            return 0

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
