"""Small formatting and parsing helpers shared across the package."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

#: exiftool's canonical timestamp shape, optionally with subseconds and offset.
_EXIF_DATETIME = re.compile(
    r"^(?P<y>\d{4})[:\-](?P<mo>\d{2})[:\-](?P<d>\d{2})"
    r"[ T](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    # The offset may be attached (exiftool) or space-separated (our own
    # format_datetime output), so round-tripping a formatted value works.
    r"\s*(?P<tz>Z|[+\-]\d{2}:?\d{2})?$"
)

_OFFSET = re.compile(r"^(?P<sign>[+\-])(?P<h>\d{2}):?(?P<m>\d{2})$")


def measured(value: float | None) -> str | None:
    """Round a physical measurement to the precision it was actually measured to.

    A GPS error radius arrives as ``21.8535`` and was printed into prose reading
    "accurate to about 21.8535 metres" — "about" and four decimals in the same
    sentence, a tenth of a millimetre of certainty about an uncertainty. One
    decimal is already generous below ten metres; above it, none is.
    """
    if value is None:
        return None
    if abs(value) >= 10 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.1f}"


#: Every C0 control plus DEL. ESC is the one that matters: rich's own
#: STRIP_CONTROL_CODES is [7, 8, 11, 12, 13], which removes the BEL that would
#: *terminate* an OSC sequence and leaves the ESC that opens it.
_CONTROL = dict.fromkeys(range(32), " ") | {0x7F: " "}

#: Bidirectional overrides reverse everything printed after them, which is how
#: "gpj.exe" is made to read as "exe.jpg". findpic's own rules call the pattern
#: out as having no legitimate reason; printing it unchallenged in a report
#: about deception would be an odd thing to do.
_BIDI = {ord(c): None for c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"}


def printable(value: object) -> str:
    """A value from a photograph, made safe to put on a terminal.

    Every camera name, lens, artist, place and filename in a report comes out of
    a file somebody else made. A control character in one of them hands that
    person the cursor: an ESC opens a sequence that repaints the line, moves the
    caret, or sets the window title, and a bidi override makes a name read
    backwards.

    ``Text.append`` does not do this — it is markup-inert, which is a different
    problem — so anything reaching the screen goes through here first, whichever
    renderer it belongs to.
    """
    text = str(value)
    # A filename does not have to be valid UTF-8. Python hands those back with
    # the undecodable bytes as lone surrogates, and writing one raises
    # UnicodeEncodeError — which killed the run part-way through a listing, with
    # the files already printed on stdout and every later file never reported.
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    return text.translate(_CONTROL).translate(_BIDI)


def human_bytes(size: float) -> str:
    """Render a byte count the way a person would say it."""
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}".replace(".0 ", " ")
    return f"{size:.1f} TB"


def parse_exif_datetime(value: Any) -> dt.datetime | None:
    """Parse an exiftool timestamp into a datetime, tz-aware when stated.

    exiftool writes zeroed placeholders (``0000:00:00 00:00:00``) for cameras
    that never had the clock set; those are not dates and are rejected.
    """
    if not isinstance(value, str):
        return None
    match = _EXIF_DATETIME.match(value.strip())
    if not match:
        return None
    parts = match.groupdict()
    try:
        year, month, day = int(parts["y"]), int(parts["mo"]), int(parts["d"])
        hour, minute, second = int(parts["h"]), int(parts["mi"]), int(parts["s"])
        if not (year and month and day):
            return None
        microsecond = 0
        if parts["frac"]:
            microsecond = int(parts["frac"][:6].ljust(6, "0"))
        tzinfo = parse_offset(parts["tz"]) if parts["tz"] else None
        return dt.datetime(year, month, day, hour, minute, second, microsecond, tzinfo=tzinfo)
    except ValueError:
        return None


def parse_offset(value: Any) -> dt.timezone | None:
    """Parse ``+02:00`` / ``-0500`` / ``Z`` into a timezone."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in {"Z", "z"}:
        return dt.timezone.utc
    match = _OFFSET.match(text)
    if not match:
        return None
    hours, minutes = int(match["h"]), int(match["m"])
    if hours > 23 or minutes > 59:
        return None
    delta = dt.timedelta(hours=hours, minutes=minutes)
    return dt.timezone(-delta if match["sign"] == "-" else delta)


def format_datetime(value: dt.datetime | None, with_offset: bool = True) -> str | None:
    if value is None:
        return None
    text = value.strftime("%Y-%m-%d %H:%M:%S")
    if with_offset and value.tzinfo is not None:
        offset = value.strftime("%z")
        if offset:
            text = f"{text} {offset[:3]}:{offset[3:]}"
    return text


def same_moment(left: dt.datetime | None, right: dt.datetime | None) -> bool | None:
    """Whether two Exif timestamps describe the same moment.

    Exif is inconsistent about time zones: ``DateTimeOriginal`` often has an
    accompanying ``OffsetTimeOriginal`` while ``ModifyDate`` has none, so the two
    parse to one aware and one naive datetime. Comparing those as strings — or
    letting Python raise on the mixed comparison — reports a difference that is
    not there. When either side is naive we compare wall-clock readings, which is
    what the Exif spec means by local time.

    Comparison is at whole-second granularity. Cameras write a sub-second
    companion tag for ``DateTimeOriginal`` but usually not for ``ModifyDate``, so
    an exact comparison reports a few hundred milliseconds of "editing" on every
    untouched photo.

    Returns ``None`` when there is nothing to compare.
    """
    if left is None or right is None:
        return None
    left = left.replace(microsecond=0)
    right = right.replace(microsecond=0)
    if (left.tzinfo is None) != (right.tzinfo is None):
        return left.replace(tzinfo=None) == right.replace(tzinfo=None)
    return left == right


def to_dms(value: float, is_latitude: bool) -> str:
    """Decimal degrees to degrees/minutes/seconds with a hemisphere letter."""
    hemisphere = ("N" if value >= 0 else "S") if is_latitude else ("E" if value >= 0 else "W")
    # Rounded to the precision that is about to be printed, *then* split.
    # Truncating degrees and minutes while rounding seconds separately never
    # propagated the carry, so 139.7 came out as 139°41'60.00"E — sixty seconds
    # is not a notation, and this is a tool people quote coordinates out of.
    total = round(abs(value) * 3600, 2)
    degrees, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{int(degrees)}°{int(minutes):02d}'{seconds:05.2f}\"{hemisphere}"


def coords_to_dms(latitude: float, longitude: float) -> str:
    return f"{to_dms(latitude, True)} {to_dms(longitude, False)}"


def truncate(value: Any, limit: int = 120) -> str | None:
    """Shorten a value for display, preserving "there was nothing here".

    None in, None out. Stringifying it produced the literal word "None", which
    is truthy — so every photograph with no colour profile ended its report
    with "Colour profile None", in both languages.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\n", "\\n").replace("\r", "\\r")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compare_geometry(
    exif: tuple[int | None, int | None], real: tuple[int | None, int | None]
) -> str | None:
    """How the picture's real size relates to the size Exif remembers.

    ``None`` when they agree or either is unreadable, else ``"rotate"``,
    ``"resize"`` or ``"crop"``.

    Shared because two rules were computing it separately and reaching different
    answers about the same four integers: on a plain downscale one said
    "resized" and the next line said "the preview may still show what was
    cropped out". They cannot now diverge, and the orientation-swap exemption —
    a lossless rotate is neither a crop nor a resize — lives in one place
    instead of being remembered twice.
    """
    exif_width, exif_height = exif
    real_width, real_height = real
    if not all((exif_width, exif_height, real_width, real_height)):
        return None
    if (exif_width, exif_height) == (real_width, real_height):
        return None
    if (exif_width, exif_height) == (real_height, real_width):
        return "rotate"
    original = exif_width / exif_height
    current = real_width / real_height
    return "resize" if abs(original - current) / original < 0.01 else "crop"
