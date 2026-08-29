"""Turning what a stranger called their file into something safe to use.

A Telegram document arrives carrying a filename its sender chose. The bot needs
that name — filename-based findings would be nonsense without it, and the clean
copy and the sidecar are handed back named after it — so it cannot simply be
discarded. It has to be made safe instead, at every point where it becomes a
path or a caption.

What arrives here is genuinely hostile. ``.`` and ``..`` are not filenames;
``Path('.').name`` is the empty string and ``with_name('')`` raises, while
``with_name('..')`` resolves to the *parent directory* and quietly moves the
upload out of the scratch space that was going to delete it. A NUL byte in the
suffix raises ``ValueError`` from deep inside ``open``, which is not an
``OSError`` and so escapes every handler written to expect one. A 4 kB name
exceeds ``NAME_MAX``. A right-to-left override makes ``photo.exe`` read as
``exe.otohp`` in the caption Telegram shows the next person.

None of that is caught by ``Path(name).name`` alone, which is what this module
exists to replace.
"""

from __future__ import annotations

from pathlib import Path

#: Suffixes the bot will accept and reuse. Anything else becomes ``.bin``: an
#: extension is a hint to the next program that opens the file, and passing an
#: unrecognised one through is how a bot hands somebody a ``.desktop``.
IMAGE_SUFFIXES = frozenset(
    {
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
)

#: Long enough for any real photo name, short enough to stay under NAME_MAX once
#: a uuid and a suffix are added to it.
MAX_NAME = 64


def safe_suffix(raw: str | None, fallback: str = ".bin") -> str:
    """The file's extension, if it is one we recognise, else ``fallback``."""
    suffix = Path(raw or "").suffix.lower()
    return suffix if suffix in IMAGE_SUFFIXES else fallback


def display_name(raw: str | None, fallback: str = "photo") -> str:
    """The sender's filename, made safe to be a real path component.

    Never returns an empty string, a directory reference, or anything holding a
    separator, a NUL, or an unprintable character.
    """
    name = Path(raw or "").name
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "/\\")
    # Leading and trailing dots are what turn a name into "." or a hidden file.
    name = name.strip().strip(".")
    if not name:
        return f"{fallback}{safe_suffix(raw, '.jpg')}"
    if len(name) <= MAX_NAME:
        return name
    # Keep the extension when trimming: it is the part the next program reads.
    suffix = Path(name).suffix[:8]
    return name[: MAX_NAME - len(suffix)] + suffix


def clean_copy_name(raw: str | None) -> str:
    """What to call the metadata-free copy handed back to the user."""
    safe = display_name(raw)
    stem = Path(safe).stem or "photo"
    return f"{stem}_clean{safe_suffix(raw, '.jpg')}"
