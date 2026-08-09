"""Find a capture time that outlived the metadata.

The tags are gone and nothing brings them back. But a timestamp is a fact about
a photograph that gets written down in more than one place, and the copies do
not all get deleted together. The commonest survivor is the filename: Android,
Google Photos, WhatsApp, Signal, Telegram and every screenshot tool since about
2010 put the date into the name, and a messenger that carefully strips the Exif
block then hands you a file called ``IMG-20230813-WA0002.jpg``.

That is the whole trick, and it is worth being precise about what it is worth:

- ``IMG_20230813_145435.jpg`` is the camera app's own record of the moment it
  wrote the file. It is as good as ``DateTimeOriginal`` and comes from the same
  clock.
- ``IMG-20230813-WA0002.jpg`` is WhatsApp's record of the day it handled the
  file. Right day, usually; the ``0002`` is a counter, not a time. Treating it
  as a capture time would put an invented hour on the picture.
- ``IMG_2781.JPG`` is a counter and nothing else. Apple has never put a date in
  a filename, and reading one out of that number would be fiction.

So every result carries how precise it actually is and where it came from, and
a caller that wants to write one back has to look at both. A filename is also
trivially changed — which makes this evidence, not proof, and the report says
so.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

#: How much of a moment a filename actually pins down.
PRECISION_SECOND = "second"
PRECISION_MINUTE = "minute"
PRECISION_DAY = "day"


@dataclass(frozen=True)
class RecoveredTime:
    """A capture time read out of somewhere other than the metadata."""

    moment: dt.datetime
    #: Catalogue key for the naming scheme this came from, e.g. "android".
    source: str
    precision: str
    #: The part of the filename the value was read from, for showing the reader.
    matched: str

    @property
    def exif_value(self) -> str:
        """The moment in Exif's own format, ready to hand to exiftool."""
        return self.moment.strftime("%Y:%m:%d %H:%M:%S")

    @property
    def is_exact(self) -> bool:
        return self.precision == PRECISION_SECOND


def _dt(*parts: str) -> dt.datetime | None:
    """Build a datetime from digit groups, rejecting impossible ones.

    Filenames contain numbers that merely look like dates — an id, a resolution,
    a phone number — so anything that is not a real calendar moment is discarded
    rather than clamped into one.
    """
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return None
    while len(values) < 6:
        values.append(0)
    year, month, day, hour, minute, second = values[:6]
    # A photograph predating the format, or dated after tomorrow, is a false
    # positive on some other number.
    if not 1990 <= year <= dt.date.today().year + 1:
        return None
    try:
        return dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


#: ``(pattern, source key, precision)``, most specific first. Each pattern must
#: capture its digit groups in year, month, day, hour, minute, second order.
PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # Android camera apps and Google Photos: IMG_20230813_145435.jpg,
    # VID_20230813_145435, PXL_20230813_145435123.
    (
        re.compile(
            r"^(?:IMG|VID|PXL|MVIMG|PANO|BURST)[_-]?(\d{4})(\d{2})(\d{2})"
            r"[_-](\d{2})(\d{2})(\d{2})",
            re.I,
        ),
        "android",
        PRECISION_SECOND,
    ),
    # Android screenshots: Screenshot_20230813-145435.png.
    (
        re.compile(
            r"^Screenshot[_ -](\d{4})(\d{2})(\d{2})[_ -]?(\d{2})(\d{2})(\d{2})",
            re.I,
        ),
        "screenshot",
        PRECISION_SECOND,
    ),
    # macOS screenshots: "Screenshot 2023-08-13 at 14.54.35.png" and the older
    # "Screen Shot 2023-08-13 at 14.54.35.png".
    (
        re.compile(
            r"^Screen ?shot (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})[.:](\d{2})[.:](\d{2})",
            re.I,
        ),
        "macos_screenshot",
        PRECISION_SECOND,
    ),
    # Telegram desktop: photo_2023-08-13_14-54-35.jpg.
    (
        re.compile(r"^photo[_-](\d{4})-(\d{2})-(\d{2})[_ -](\d{2})-(\d{2})-(\d{2})", re.I),
        "telegram",
        PRECISION_SECOND,
    ),
    # Signal: signal-2023-08-13-145435.jpg, and the newer -14-54-35 form.
    (
        re.compile(r"^signal[_-](\d{4})-(\d{2})-(\d{2})[_-](\d{2})-?(\d{2})-?(\d{2})", re.I),
        "signal",
        PRECISION_SECOND,
    ),
    # WhatsApp desktop: "WhatsApp Image 2023-08-13 at 14.54.35.jpeg".
    (
        re.compile(
            r"^WhatsApp (?:Image|Video) (\d{4})-(\d{2})-(\d{2}) at "
            r"(\d{1,2})[.](\d{2})[.](\d{2})",
            re.I,
        ),
        "whatsapp",
        PRECISION_SECOND,
    ),
    # Bare timestamps: 20230813_145435.jpg, 2023-08-13 14.54.35.jpg.
    (
        re.compile(r"^(\d{4})[-_]?(\d{2})[-_]?(\d{2})[ _T-](\d{2})[-.:]?(\d{2})[-.:]?(\d{2})"),
        "timestamped",
        PRECISION_SECOND,
    ),
    # WhatsApp mobile: IMG-20230813-WA0002.jpg. The trailing number is a
    # per-day counter, so this is the right day and no hour at all.
    (
        re.compile(r"^(?:IMG|VID|AUD|PTT)-(\d{4})(\d{2})(\d{2})-WA\d+", re.I),
        "whatsapp",
        PRECISION_DAY,
    ),
    # Date-only names: 2023-08-13.jpg, 20230813.jpg.
    (re.compile(r"^(\d{4})[-_]?(\d{2})[-_]?(\d{2})(?:\D|$)"), "timestamped", PRECISION_DAY),
)


def timestamp_from_filename(name: str) -> RecoveredTime | None:
    """Read a capture time out of a filename, or return None.

    None is the common and correct answer. ``IMG_2781.JPG`` is a counter, and a
    function willing to guess at it would be worse than useless — it would put a
    confident wrong date on a photograph.
    """
    for pattern, source, precision in PATTERNS:
        match = pattern.match(name)
        if not match:
            continue
        moment = _dt(*match.groups())
        if moment is None:
            continue
        return RecoveredTime(
            moment=moment,
            source=source,
            precision=precision,
            matched=match.group(0),
        )
    return None
