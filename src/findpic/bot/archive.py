"""Keeping a copy of every picture the bot is sent.

The operator of a bot that reads photographs wants to see what it is being used
on — which pictures, from whom, and what came of them. Nothing survived a
request before this: the working copy is unlinked in a ``finally`` as soon as
the analysis returns, and a separate janitor deletes the Bot API server's own
copy five minutes later.

**The layout, and why it is this one.**

::

    objects/3f/9a/3f9a2c1b….heic                              the bytes, one copy
    by-date/2026-08-29/20260829T134501Z-u7332288724-3f9a2c1b.heic   a hardlink
    .tmp/                                                     in flight

``objects/`` is content-addressed and fans out on the first two byte-pairs of
the digest, so no directory ever holds more than a few hundred entries.
``by-date/`` is what an admin actually browses: one directory per UTC day, names
that sort chronologically and carry the moment, the sender and the first eight
hex of the digest — so ``grep 3f9a2c1b`` joins a file on disk to a row in the
manifest to a line in the report. The two are hardlinks to one inode, so a photo
sent twice costs one copy of the bytes and still shows as two entries.

**No byte of attacker-controlled text ever enters a path.** Every name is built
from a UTC timestamp, an integer user id, a hex digest and an allowlisted
extension. Path traversal, NUL bytes, right-to-left overrides, 4 kB names,
Windows reserved names and duplicate names are not defended against here; they
are made impossible by construction. What the sender called their file is kept
only as a column in the database, rendered through escaping on the way out.

**Nothing here raises.** A failure to keep a copy must never become a failure to
answer the user: a full disk would otherwise turn into "the bot is broken" for
everybody. Every outcome — including the refusals — comes back as a
:class:`Stored` with a state, and the state is recorded, because an archive
whose failures are invisible is worse than no archive at all. The operator would
believe photographs were being kept for weeks before finding out they were not.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Read in blocks rather than whole; a 64 MB file should not become 64 MB of
#: heap just to be hashed.
CHUNK = 1024 * 1024

#: Directory names under the archive root.
OBJECTS = "objects"
BY_DATE = "by-date"
SCRATCH = ".tmp"

#: What the first bytes of a file say it is, which is the only trustworthy
#: source for an extension. exiftool's own detection is not available yet at the
#: moment the copy is taken — deliberately, because a file that crashes exiftool
#: is exactly the one an admin most wants on disk to reproduce with.
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
)

#: ISO base media brands, read at offset 4 after "ftyp". The HEIF list is what
#: iPhones actually emit; `heic` alone misses most of them.
BRANDS: dict[bytes, str] = {
    b"heic": ".heic",
    b"heix": ".heic",
    b"hevc": ".heic",
    b"heim": ".heic",
    b"heis": ".heic",
    b"mif1": ".heic",
    b"msf1": ".heic",
    b"avif": ".avif",
    b"qt  ": ".mov",
    b"mp41": ".mp4",
    b"mp42": ".mp4",
    b"isom": ".mp4",
}

#: A plausible extension: a dot and a few alphanumerics, nothing else.
_CLAIMED = re.compile(r"\.[A-Za-z0-9]{1,8}")

#: How long to stop trying after a structural failure. Without it a full disk
#: produces one stack trace per photograph and drowns the log.
COOLDOWN_SECONDS = 300

#: The errno values that mean "this will not work again in a moment".
STRUCTURAL = frozenset({errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM, errno.EDQUOT})


@dataclass(frozen=True)
class Stored:
    """What became of one attempt to keep a picture.

    ``state`` is always set, including for the refusals, so the manifest can
    record that a file was deliberately not kept rather than leaving a hole
    that reads as an unexplained gap.
    """

    state: str
    sha256: str | None = None
    size: int = 0
    rel_path: str | None = None
    error: str | None = None

    @property
    def kept(self) -> bool:
        return self.state in ("stored", "duplicate")


def _safe_claim(suffix: str) -> str:
    """A caller's suggested extension, or ``.bin``.

    Validated here rather than trusted from the one caller that happens to
    sanitise it today. Without this, bytes whose magic is unrecognised plus a
    claim of "/../../../outside.sh" builds the traversal with
    ``mkdir(parents=True)`` and lands the file before the containment check
    further down ever runs — while this module's own docstring claims that is
    impossible by construction. It is, now.
    """
    return suffix if _CLAIMED.fullmatch(suffix or "") else ".bin"


def sniff(data: bytes, fallback: str = ".bin") -> str:
    """The extension the file's own first bytes justify."""
    for magic, suffix in MAGIC:
        if data.startswith(magic):
            return suffix
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return BRANDS.get(data[8:12], ".bin")
    return fallback


class Archive:
    """A content-addressed store on disk, with a browsable index beside it."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 32 * 1024 * 1024,
        max_total_bytes: int = 4 * 1024 * 1024 * 1024,
        max_user_bytes: int = 512 * 1024 * 1024,
        min_free_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self.root = Path(root)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_user_bytes = max_user_bytes
        self.min_free_bytes = min_free_bytes
        self._paused_until = 0.0

    # ------------------------------------------------------------ lifecycle

    def prepare(self) -> str | None:
        """Create the directories and prove they can be written to.

        Returns None on success, or a sentence naming the problem. Called at
        startup rather than on the first photograph, because a silently
        non-functioning archive is the worst outcome available: the operator
        would believe pictures were being kept and only discover otherwise when
        they went looking for one.
        """
        try:
            for name in (OBJECTS, BY_DATE, SCRATCH):
                (self.root / name).mkdir(parents=True, exist_ok=True)
            probe = self.root / SCRATCH / f"probe-{uuid.uuid4().hex}"
            probe.write_bytes(b"findpic")
            probe.unlink()
        except OSError as error:
            return (
                f"cannot write to {self.root}: {error}. The container runs as uid "
                f"{os.getuid()}; if this is a bind mount, the host directory has to be "
                f"owned by that uid — Docker creates a missing one as root."
            )
        self.sweep_scratch()
        return None

    def sweep_scratch(self) -> int:
        """Remove in-flight files a crash left behind."""
        removed = 0
        scratch = self.root / SCRATCH
        if not scratch.is_dir():
            return 0
        for path in scratch.iterdir():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    # ---------------------------------------------------------------- write

    def store(
        self,
        source: Path,
        *,
        user_id: int,
        when: str,
        claimed_suffix: str = "",
        held_bytes: int = 0,
        user_bytes: int = 0,
    ) -> Stored:
        """Keep a copy of ``source``. Never raises.

        ``when`` is a UTC ``YYYYMMDDTHHMMSSZ`` stamp supplied by the caller, so
        the name is reproducible and this stays testable without freezing a clock.
        """
        if time.monotonic() < self._paused_until:
            return Stored(state="skipped_space", error="archiving is paused after a disk error")
        try:
            return self._store(
                source,
                user_id=user_id,
                when=when,
                claimed_suffix=claimed_suffix,
                held_bytes=held_bytes,
                user_bytes=user_bytes,
            )
        except OSError as error:
            self._maybe_pause(error)
            logger.warning("could not archive a file: %s", error)
            return Stored(state="error", error=str(error))
        except (ValueError, shutil.Error) as error:
            # shutil.SameFileError is a shutil.Error, and a bad path raises
            # ValueError rather than OSError — neither would be caught above.
            logger.warning("could not archive a file: %s", error)
            return Stored(state="error", error=str(error))

    def _store(
        self,
        source: Path,
        *,
        user_id: int,
        when: str,
        claimed_suffix: str,
        held_bytes: int,
        user_bytes: int,
    ) -> Stored:
        size = source.stat().st_size
        if self.max_file_bytes and size > self.max_file_bytes:
            return Stored(state="skipped_big", size=size)
        if self.max_user_bytes and user_bytes + size > self.max_user_bytes:
            return Stored(state="skipped_space", size=size, error="per-user limit reached")
        if not self._has_room(size, held_bytes):
            return Stored(state="skipped_space", size=size, error="no room on the volume")

        digest, head = self._digest(source)
        # The blob's name comes from the bytes and nothing else. Letting the
        # sender's claimed extension into it meant one photograph occupied as
        # many blobs as the sender chose names — sniff() falls through to the
        # claim for anything whose magic it does not know, which is every raw
        # format and WebP — so dedup missed every time, the disk cap
        # under-counted, and discarding the visible name left full copies of
        # the picture in objects/ that nothing referenced and nothing would
        # ever delete, while the retention window promises otherwise.
        blob = self.root / OBJECTS / digest[:2] / digest[2:4] / f"{digest}{sniff(head)}"
        # The claim survives only on the browsable name, where it is a hint to
        # whatever opens the file and cannot affect what is stored where.
        suffix = sniff(head, _safe_claim(claimed_suffix))

        duplicate = blob.exists()
        if not duplicate:
            self._write(source, blob)

        link = self._link(blob, when=when, user_id=user_id, digest=digest, suffix=suffix)
        return Stored(
            state="duplicate" if duplicate else "stored",
            sha256=digest,
            size=size,
            rel_path=str(link.relative_to(self.root)),
        )

    def _has_room(self, size: int, held_bytes: int) -> bool:
        if self.max_total_bytes and held_bytes + size > self.max_total_bytes:
            return False
        if not self.min_free_bytes:
            return True
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:
            return True  # cannot tell; the write will say so if it cannot
        return free - size >= self.min_free_bytes

    def _digest(self, source: Path) -> tuple[str, bytes]:
        """The file's sha256 and its first bytes, in one pass over the disk."""
        sha = hashlib.sha256()
        head = b""
        with source.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                if not head:
                    head = chunk[:16]
                sha.update(chunk)
        return sha.hexdigest(), head

    def _write(self, source: Path, blob: Path) -> None:
        """Copy into place, atomically enough that a crash leaves no half file."""
        blob.parent.mkdir(parents=True, exist_ok=True)
        scratch = self.root / SCRATCH
        scratch.mkdir(parents=True, exist_ok=True)
        temp = scratch / uuid.uuid4().hex
        try:
            # copyfile rather than a rename: the source is on a tmpfs, a
            # different filesystem, so a rename would fail across the boundary.
            shutil.copyfile(source, temp)
            with temp.open("rb") as handle:
                os.fsync(handle.fileno())
            temp.chmod(0o440)
            # Another request may have stored the same bytes while this one was
            # copying. Both are correct; the first wins and this copy is dropped.
            with contextlib.suppress(FileExistsError):
                os.link(temp, blob)
        finally:
            temp.unlink(missing_ok=True)

    def _link(self, blob: Path, *, when: str, user_id: int, digest: str, suffix: str) -> Path:
        """The browsable name, in the day's directory.

        The counter is not decoration. The stamp is per-second, so one person
        sending the same picture twice inside a second collides — and an admin
        is exempt from the throttle entirely, so for the operator's own account
        that is an ordinary thing to do rather than a race.
        """
        day = self.root / BY_DATE / f"{when[:4]}-{when[4:6]}-{when[6:8]}"
        day.mkdir(parents=True, exist_ok=True)
        stem = f"{when}-u{user_id}-{digest[:8]}"
        for attempt in range(100):
            suffix_n = "" if attempt == 0 else f"-{attempt:02d}"
            link = day / f"{stem}{suffix_n}{suffix}"
            try:
                os.link(blob, link)
                return link
            except FileExistsError:
                continue
            except OSError:
                # Hard links unavailable on this filesystem. A second copy costs
                # disk but keeps the archive browsable, which is the point.
                shutil.copyfile(blob, link)
                return link
        return blob

    def _maybe_pause(self, error: OSError) -> None:
        """Stop trying for a while after a failure that will not clear itself."""
        if error.errno in STRUCTURAL:
            self._paused_until = time.monotonic() + COOLDOWN_SECONDS
            logger.error(
                "archiving is paused for %s seconds after: %s. The container runs as "
                "uid %s; a bind mount's host directory must be owned by that uid.",
                COOLDOWN_SECONDS,
                error,
                os.getuid(),
            )

    # ---------------------------------------------------------------- read

    def resolve(self, rel_path: str) -> Path | None:
        """A stored path, checked to be inside the archive.

        The manifest is trusted less than it looks: it is a database the bot
        writes, but a report or a future admin command reads paths back out of
        it, and one containment check here is cheaper than remembering to do it
        at every call site.
        """
        candidate = (self.root / rel_path).resolve()
        root = self.root.resolve()
        return candidate if candidate.is_relative_to(root) and candidate.exists() else None

    def discard(self, rel_path: str) -> int | None:
        """Remove one kept picture, and its bytes if nothing else references them.

        Returns the bytes freed, or None when the file is still there, which a
        caller has to be able to tell apart from "there was nothing to remove":
        the two mean opposite things to whatever is keeping the ledger. The link count is what makes this
        safe: a photograph two people sent has two names and one inode, and
        deleting the inode when the first is evicted would take the second
        person's picture with it.
        """
        try:
            link = self.resolve(rel_path)
        except ValueError:
            # A NUL in the path reaches the C layer as a ValueError, which is
            # not an OSError and so escapes the handler below.
            return None
        if link is None:
            return 0  # already gone, which is the outcome the caller wanted
        try:
            info = link.stat()
            blob = self._blob_for(link)
            link.unlink()
            freed = 0
            if blob is not None and blob.exists() and blob.stat().st_nlink == 1:
                freed = blob.stat().st_size
                blob.unlink()
            elif info.st_nlink <= 1:
                freed = info.st_size
            return freed
        except OSError as error:
            logger.warning("could not remove %s: %s", rel_path, error)
            return None

    def _blob_for(self, link: Path) -> Path | None:
        """The content-addressed copy a browsable name points at.

        The name is ``{stamp}-u{user_id}-{digest8}`` with an optional ``-NN``
        collision counter, so the digest is the **third** field and not the last
        one. Taking the last was wrong in exactly the case the counter exists
        for: a second send inside the same second produced ``…-3f9a2c1b-01.jpg``,
        whose last field is ``01``, so eviction could not find the blob and
        leaked its bytes permanently. An admin is exempt from the throttle, so
        for the operator's own account that is ordinary rather than a race.

        Only eight hex characters are in the name, so the blob is found through
        the directory fan-out rather than reconstructed.
        """
        fields = link.stem.split("-")
        if len(fields) < 3:
            return None
        digest = fields[2]
        if len(digest) != 8 or any(character not in "0123456789abcdef" for character in digest):
            return None
        bucket = self.root / OBJECTS / digest[:2] / digest[2:4]
        if not bucket.is_dir():
            return None
        # The browsable name carries only the first eight hex characters, so the
        # full digest cannot be reconstructed from it — hence the scan. It is
        # unambiguous now that a blob's extension comes from its own bytes:
        # before, one photograph could occupy several blobs under one prefix.
        return next((path for path in bucket.iterdir() if path.name.startswith(digest)), None)
