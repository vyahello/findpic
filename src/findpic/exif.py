"""Hardened wrapper around the `exiftool` binary.

Design notes, because the details here are the difference between a safe tool and
a shell-injection hole:

* We build an argv **list** and never use ``shell=True``.
* Paths are resolved to absolute before being passed. That kills two problems at
  once: a filename beginning with ``-`` can no longer be read as an option, and
  we never depend on the process working directory.
* exiftool accepts ``-execute`` on the plain command line, not just in an argfile.
  So we get all three passes (human-readable, numeric, validation) out of a
  single process — roughly half the latency of spawning twice — while still
  passing the filename as a normal argv element rather than an argfile line,
  where leading/trailing whitespace would be stripped.
* Every run is bounded by a timeout and the file by a size ceiling, so a hostile
  or pathological input cannot wedge the caller.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import truncate

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_BYTES = 512 * 1024 * 1024  # 512 MiB

#: exiftool's placeholder for a value it will not inline into JSON.
BINARY_MARKER = "(Binary data"


class ExifToolError(RuntimeError):
    """Base class for every failure originating in the extraction layer."""


#: exiftool's end-of-command marker, numbered or not.
_READY = re.compile(r"\{ready\d*\}")


class ExifToolMissing(ExifToolError):
    """The exiftool binary is not installed or not on PATH."""


class ExifToolTimeout(ExifToolError):
    """exiftool did not finish within the allotted time."""


class UnreadableFile(ExifToolError):
    """The path is missing, is not a regular file, or exceeds the size ceiling."""


def _is_binary_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(BINARY_MARKER)


def _iter_json_documents(text: str) -> Iterator[Any]:
    """Yield each JSON document from a stream of concatenated documents.

    ``-execute`` makes exiftool emit one JSON array per block, back to back with
    no separator, which ``json.loads`` cannot parse in one go.
    """
    decoder = json.JSONDecoder()
    index, length = 0, len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            return
        try:
            document, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            return
        yield document
        index = end


@dataclass
class Metadata:
    """Every tag exiftool could read, in both human and numeric form.

    Tags are keyed ``Group:Tag`` (``IFD0:Model``). Lookups accept either the full
    key or a bare tag name; a bare name matches the first group that carries it,
    which is what you want in practice since exiftool's group precedence already
    puts the authoritative copy first.
    """

    path: str
    human: dict[str, Any] = field(default_factory=dict)
    numeric: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: ``(exit code, blocks parsed, blocks asked for)`` when exiftool did not
    #: finish. ``None`` on a complete read. Rules must be able to tell "this
    #: file has no GPS" apart from "we stopped looking before the GPS block".
    incomplete: tuple[int, int, int] | None = None
    _index: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _lowered: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._reindex()

    def _reindex(self) -> None:
        """Index both passes.

        The two passes do not carry identical key sets: ``-struct`` collapses the
        XMP structures in the human pass, while ``-n`` leaves them flat. Indexing
        only one of them would make the other's exclusive tags unreachable.
        """
        index: dict[str, list[str]] = {}
        lowered: dict[str, str] = {}
        for source in (self.human, self.numeric):
            for key in source:
                bare = key.split(":", 1)[-1].lower()
                bucket = index.setdefault(bare, [])
                if key not in bucket:
                    bucket.append(key)
                lowered.setdefault(key.lower(), key)
        self._index = index
        self._lowered = lowered

    # ------------------------------------------------------------------ lookup

    def _resolve(self, name: str) -> str | None:
        """Map a user-supplied tag name onto a key present in either pass."""
        if name in self.human or name in self.numeric:
            return name
        lowered = name.lower()
        exact = self._lowered.get(lowered)
        if exact is not None:
            return exact
        if ":" in name:
            # Allow a group-qualified miss to fall through to a bare-name match,
            # so callers can name the group they expect without being punished
            # when exiftool filed the tag under a sibling group.
            lowered = lowered.split(":", 1)[-1]
        candidates = self._index.get(lowered)
        return candidates[0] if candidates else None

    def get(self, *names: str, default: Any = None) -> Any:
        """First present, non-empty human-readable value among ``names``."""
        for name in names:
            key = self._resolve(name)
            if key is None:
                continue
            value = self.human.get(key)
            if value is None or value == "":
                continue
            return value
        return default

    def num(self, *names: str, default: Any = None) -> Any:
        """First present numeric (``-n``) value among ``names``."""
        for name in names:
            key = self._resolve(name)
            if key is None:
                continue
            value = self.numeric.get(key)
            if value is None or value == "":
                continue
            return value
        return default

    def float(self, *names: str, default: float | None = None) -> float | None:
        value = self.num(*names)
        if value is None:
            value = self.get(*names)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def int(self, *names: str, default: int | None = None) -> int | None:
        value = self.float(*names)
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def str(self, *names: str, default: str | None = None) -> str | None:
        """First scalar value among ``names``, as displayable text.

        Structures and lists are skipped rather than stringified. exiftool keeps
        several copies of some tags — an XMP structure alongside a flat Composite
        value, for instance — and rendering the structure would print a raw Python
        dict at the user. So we walk every group carrying the name and return the
        first one that is actually a scalar.
        """
        located = self.located(*names)
        return located[1] if located else default

    def located(self, *names: str) -> tuple[str, str] | None:
        """Like :meth:`str`, but says which key the value actually came from.

        A caller asking for ``IPTC:Source`` can be answered out of
        ``XMP-dc:Source``, because a group-qualified miss falls back to a bare
        name. Reporting the name that was *asked for* sent readers to delete a
        tag the file does not have — and the value they wanted to remove stayed
        exactly where it was.
        """
        for name in names:
            for key in self._candidates(name):
                value = self.human.get(key, self.numeric.get(key))
                if value is None or isinstance(value, (dict, list)):
                    continue
                text = value.strip() if isinstance(value, str) else str(value)
                if text:
                    return key, text
        return None

    def _candidates(self, name: str) -> list[str]:
        """Every key that could satisfy ``name``, best match first."""
        if name in self.human or name in self.numeric:
            return [name]
        exact = self._lowered.get(name.lower())
        if exact is not None:
            return [exact]
        bare = name.split(":", 1)[-1].lower()
        return list(self._index.get(bare, []))

    def has(self, *names: str) -> bool:
        return any(self._resolve(name) is not None for name in names)

    def keys_for(self, name: str) -> list[str]:
        """Every group-qualified key carrying this bare tag name."""
        return list(self._index.get(name.lower(), []))

    # ------------------------------------------------------------------ groups

    @property
    def all_keys(self) -> list[str]:
        """Union of both passes, human order first."""
        return list(dict.fromkeys([*self.human, *self.numeric]))

    def group(self, group: str) -> dict[str, Any]:
        prefix = f"{group.lower()}:"
        return {
            k: self.human.get(k, self.numeric.get(k))
            for k in self.all_keys
            if k.lower().startswith(prefix)
        }

    def group_names(self) -> set[str]:
        return {k.split(":", 1)[0] for k in self.all_keys if ":" in k}

    def has_group(self, *groups: str) -> bool:
        present = {g.lower() for g in self.group_names()}
        return any(g.lower() in present for g in groups)

    def group_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for key in self.all_keys:
            group = key.split(":", 1)[0] if ":" in key else "Other"
            counts[group] = counts.get(group, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    # ------------------------------------------------------------------- views

    def text_items(
        self, skip_groups: tuple[str, ...] = ("System", "ExifTool")
    ) -> Iterator[tuple[str, str]]:
        """Every human-readable string value, for content scanning.

        Binary placeholders and the groups that only describe the filesystem are
        skipped — they are noise for anyone hunting for smuggled payloads.
        """
        skip = tuple(f"{g.lower()}:" for g in skip_groups)
        for key in self.all_keys:
            if key.lower().startswith(skip):
                continue
            value = self.human.get(key, self.numeric.get(key))
            if isinstance(value, str) and not _is_binary_placeholder(value):
                yield key, value

    @property
    def tag_count(self) -> int:
        return len(self.all_keys)

    def is_binary(self, name: str) -> bool:
        return _is_binary_placeholder(self.get(name))


class ExifTool:
    """Runs the exiftool binary and returns parsed :class:`Metadata`."""

    #: Shared across every pass: read duplicates (-a) and unknown tags (-u), keep
    #: group-1 names so we can tell IFD0 from ExifIFD, and never let exiftool
    #: interpret the filename charset differently from what Python handed it.
    BASE_ARGS = (
        "-json",
        "-G1",
        "-a",
        "-u",
        "-charset",
        "filename=UTF8",
        "-api",
        "LargeFileSupport=1",
    )

    def __init__(
        self,
        binary: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        persistent: bool = False,
    ) -> None:
        self.binary = binary or shutil.which("exiftool") or "exiftool"
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._process: subprocess.Popen[bytes] | None = None
        self._sequence = 0
        self._stderr_buffer: list[str] = []
        self._stderr_lock = threading.Lock()
        self._errors_thread: threading.Thread | None = None
        if persistent:
            self._start_persistent()

    # -------------------------------------------------------------- discovery

    @classmethod
    def available(cls, binary: str | None = None) -> bool:
        return shutil.which(binary or "exiftool") is not None

    def version(self) -> str:
        try:
            result = subprocess.run(
                [self.binary, "-ver"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ExifToolMissing(
                "exiftool was not found. Install it with: sudo apt install libimage-exiftool-perl"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExifToolTimeout("exiftool -ver timed out") from exc
        return result.stdout.strip()

    # ------------------------------------------------------------------ public

    def read(self, path: str | os.PathLike[str], validate: bool = True) -> Metadata:
        """Extract metadata for one file.

        Three passes in a single process: human-readable values, numeric values,
        and (optionally) exiftool's own structural validation.
        """
        target = self._checked_path(path)
        argv, expected = self._build_argv(target, validate=validate)
        # The persistent process first, the one-shot form if it is not running or
        # has stopped. The argument list is the same either way apart from the
        # binary name, which -stay_open does not want repeated.
        result = self._run_persistent(argv[1:]) if self._process is not None else None
        if result is None:
            result = self._run(argv)
        stdout, stderr, code = result

        documents = list(_iter_json_documents(stdout))
        if not documents:
            # Previously this fell through to an empty Metadata, and the
            # empty-metadata rules read it as a photograph with nothing in it.
            # Nothing parsed means nothing is known, which is not a report.
            raise ExifToolError(
                f"exiftool returned no readable output for {target} "
                f"(exit status {code})"
                + (f": {truncate(stderr.strip(), 200)}" if stderr.strip() else "")
            )
        human = self._first_record(documents, 0)
        numeric = self._first_record(documents, 1)
        validation = self._first_record(documents, 2) if validate else {}
        on_demand = self._first_record(documents, 3 if validate else 2)

        # Fold the on-demand tags into the main view; they are ordinary tags that
        # simply had to be requested by name.
        for key, value in on_demand.items():
            if key != "SourceFile":
                human.setdefault(key, value)

        metadata = Metadata(
            path=str(target),
            human=human,
            numeric=numeric,
            validation=validation,
        )
        metadata.warnings = self._collect_warnings(human, validation, stderr)
        metadata.errors = self._collect_errors(human, stderr)
        if not human and not metadata.errors:
            metadata.errors.append("exiftool returned no metadata for this file.")
        # A non-zero status, or fewer blocks than were asked for, means whole
        # passes are missing from everything below. Recorded rather than raised:
        # what did arrive is still worth showing, as long as the report says so.
        if code != 0 or len(documents) < expected:
            metadata.incomplete = (code, len(documents), expected)
        return metadata

    def extract_binary(self, path: str | os.PathLike[str], tag: str) -> bytes | None:
        """Pull one embedded binary tag (thumbnail, preview…) straight to memory."""
        target = self._checked_path(path)
        safe_tag = tag.lstrip("-")
        if not safe_tag.replace(":", "").isalnum():
            raise ValueError(f"refusing to pass a non-alphanumeric tag name: {tag!r}")
        argv = [self.binary, "-b", f"-{safe_tag}", str(target)]
        try:
            result = subprocess.run(argv, capture_output=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ExifToolTimeout(f"exiftool timed out extracting {tag}") from exc
        return result.stdout or None

    # ----------------------------------------------------------------- helpers

    def _checked_path(self, path: str | os.PathLike[str]) -> Path:
        target = Path(path).expanduser()
        try:
            target = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UnreadableFile(f"cannot resolve path: {path}") from exc
        if not target.is_file():
            raise UnreadableFile(f"not a regular file: {target}")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise UnreadableFile(f"cannot stat file: {target}") from exc
        if size > self.max_bytes:
            raise UnreadableFile(
                f"file is {size / 1024 / 1024:.0f} MB, over the "
                f"{self.max_bytes / 1024 / 1024:.0f} MB ceiling"
            )
        if size == 0:
            raise UnreadableFile(f"file is empty: {target}")
        # Everything above this line asks the *directory* about the file. None of
        # it opens it, so a mode-000 photograph passed every check, exiftool
        # returned nothing, and the empty-metadata rules graded it
        # "Privacy: CLEAN — this file carries almost no metadata, so there is
        # nothing to leak". A verdict must never be printed about bytes that
        # were never read; one byte is enough to find that out.
        try:
            with open(target, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise UnreadableFile(f"cannot read file: {target}") from exc
        return target

    #: Tags exiftool computes only when asked by name. They cannot be mixed into
    #: a general dump — naming any tag restricts the output to that tag — so they
    #: get their own -execute block. JPEGDigest fingerprints the encoder from its
    #: quantization tables, which is one of the few authenticity signals that
    #: survives a full metadata rewrite.
    ON_DEMAND_TAGS = ("-JPEGDigest", "-JPEGQualityEstimate")

    def _build_argv(self, target: Path, validate: bool) -> tuple[list[str], int]:
        """The argument vector, and the number of result blocks it asks for.

        Returned together rather than letting the caller count ``-execute``
        markers: the two must agree, and one of them being derived from the
        other is what keeps them agreeing when a pass is added.
        """
        name = str(target)
        argv = [self.binary, *self.BASE_ARGS, "-struct", name, "-execute"]
        argv += [*self.BASE_ARGS, "-n", name, "-execute"]
        if validate:
            argv += ["-json", "-G1", "-validate", "-warning", "-a", name, "-execute"]
        argv += ["-json", "-G1", *self.ON_DEMAND_TAGS, name, "-execute"]
        return argv, argv.count("-execute")

    # ------------------------------------------------------- persistent mode

    def _start_persistent(self) -> bool:
        """Spawn one exiftool that stays open for the whole run.

        exiftool is Perl, and starting the interpreter dominates: measured on
        this project's own workload — sixty copies of one photograph, the same
        four argument blocks — a process per file costs 8.6 s where a single
        persistent one costs about a fifth of that. `-stay_open` has been in
        exiftool for over a decade, and `_build_argv` already speaks the
        `-execute` protocol it needs.

        Returns False if it cannot be started, and the caller falls back to a
        process per file. Nothing here is allowed to make findpic stop working.
        """
        try:
            self._process = subprocess.Popen(  # noqa: S603
                [self.binary, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            self._process = None
            return False
        self._errors_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._errors_thread.start()
        # Registered rather than left to garbage collection: an exiftool waiting
        # on a closed stdin is a process the user did not ask to keep.
        atexit.register(self.close)
        return True

    def _drain_stderr(self) -> None:
        """Keep stderr empty, so a chatty file cannot fill the pipe and block."""
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in iter(process.stderr.readline, b""):
            with self._stderr_lock:
                self._stderr_buffer.append(line.decode("utf-8", errors="replace"))

    def close(self) -> None:
        """Shut the persistent process down, if there is one."""
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write(b"-stay_open\nFalse\n")
                process.stdin.flush()
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()

    def _run_persistent(self, argv: list[str]) -> tuple[str, str, int] | None:
        """One command through the open process, or None to fall back.

        The marker protocol is exiftool's own: everything up to ``{ready<n>}`` on
        stdout belongs to command <n>.
        """
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            return None
        if process.poll() is not None:
            return None
        # `-@ -` is a line-based protocol: one argument per line, so an argument
        # containing a newline cannot be expressed in it at all — exiftool would
        # read it as two arguments and report two files not found. Rare, and the
        # one-shot path takes those.
        if any("\n" in argument or "\r" in argument for argument in argv):
            return None

        self._sequence += 1
        token = self._sequence
        with self._stderr_lock:
            self._stderr_buffer.clear()
        payload = "\n".join([*argv, f"-execute{token}"]) + "\n"
        deadline = time.monotonic() + self.timeout
        try:
            process.stdin.write(payload.encode("utf-8", errors="surrogateescape"))
            process.stdin.flush()
        except (OSError, ValueError):
            self.close()
            return None

        marker = f"{{ready{token}}}"
        collected: list[str] = []
        while True:
            if time.monotonic() > deadline:
                # A file that hangs must not hang every file after it.
                self.close()
                raise ExifToolTimeout(f"exiftool did not finish within {self.timeout}s")
            line = process.stdout.readline()
            if not line:
                self.close()
                return None
            text = line.decode("utf-8", errors="replace")
            if text.strip() == marker:
                break
            # The argument list already contains bare `-execute` separators —
            # that is how the four passes are batched — and under -stay_open each
            # one emits its own `{ready}` into the stream. They are protocol, not
            # output, and they sit between the JSON documents the caller parses.
            if _READY.fullmatch(text.strip()):
                continue
            collected.append(text)
        with self._stderr_lock:
            errors = "".join(self._stderr_buffer)
        # -stay_open gives no per-command exit status; exiftool's own errors
        # reach us on stderr, which _collect_errors already reads.
        return "".join(collected), errors, 0

    def _run(self, argv: list[str]) -> tuple[str, str, int]:
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ExifToolMissing(
                "exiftool was not found. Install it with: sudo apt install libimage-exiftool-perl"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExifToolTimeout(f"exiftool did not finish within {self.timeout}s") from exc
        # Exif strings are frequently mislabelled or outright broken; never let a
        # bad byte take down the analysis.
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        return stdout, stderr, result.returncode

    @staticmethod
    def _first_record(documents: list[Any], position: int) -> dict[str, Any]:
        if position >= len(documents):
            return {}
        document = documents[position]
        if isinstance(document, list) and document:
            record = document[0]
            return record if isinstance(record, dict) else {}
        if isinstance(document, dict):
            return document
        return {}

    @staticmethod
    def _collect_warnings(
        human: dict[str, Any], validation: dict[str, Any], stderr: str
    ) -> list[str]:
        warnings: list[str] = []
        for source in (human, validation):
            for key, value in source.items():
                if key.split(":", 1)[-1].lower() != "warning":
                    continue
                items = value if isinstance(value, list) else [value]
                warnings.extend(str(i) for i in items)
        for line in stderr.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("warning:"):
                warnings.append(stripped[len("warning:") :].strip())
        # Preserve order while dropping repeats across the passes.
        return list(dict.fromkeys(w for w in warnings if w))

    @staticmethod
    def _collect_errors(human: dict[str, Any], stderr: str) -> list[str]:
        errors: list[str] = []
        for key, value in human.items():
            if key.split(":", 1)[-1].lower() == "error":
                errors.append(str(value))
        for line in stderr.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("error:"):
                errors.append(stripped[len("error:") :].strip())
        return list(dict.fromkeys(e for e in errors if e))


def hash_file(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> tuple[str, str]:
    """Return ``(sha256, md5)`` for a file, streamed so size does not matter."""
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()
