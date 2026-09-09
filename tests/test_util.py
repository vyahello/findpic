"""Tests for the parsing and formatting helpers."""

from __future__ import annotations

import pytest

from findpic.util import (
    measured,
    parse_exif_datetime,
    same_moment,
    to_dms,
    truncate,
)


def test_truncate() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("a" * 50, 10).endswith("…")
    assert len(truncate("a" * 50, 10)) == 10
    assert truncate("line\nbreak") == "line\\nbreak"


def test_same_moment_ignores_offset_and_subsecond_noise() -> None:
    """Both were real bugs: a naive/aware mix and a 220 ms subsecond gap."""
    aware = parse_exif_datetime("2021:02:27 22:23:42.220+02:00")
    naive = parse_exif_datetime("2021:02:27 22:23:42")
    assert same_moment(aware, naive) is True
    assert same_moment(aware, parse_exif_datetime("2024:01:02 09:00:00")) is False
    assert same_moment(aware, None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (139.7, "139°42'00.00\"E"),
        (0.9999999, "1°00'00.00\"E"),
        (49.9999999, "50°00'00.00\"E"),
        (2.2945, "2°17'40.20\"E"),
        (-0.1275, "0°07'39.00\"W"),
    ],
)
def test_dms_carries_into_minutes_and_degrees(value: float, expected: str) -> None:
    """Degrees and minutes truncated while seconds rounded, and 139.7 printed
    139°41'60.00"E. Sixty seconds is not a notation, and this is a tool people
    quote coordinates out of."""
    assert to_dms(value, is_latitude=False) == expected


def test_altitude_below_sea_level_prints_unsigned() -> None:
    """exiftool signs the value for GPSAltitudeRef=1 and the renderer adds the
    word, so a Dead Sea photo read "-413.2 m below sea level" — above it."""
    from findpic.i18n import Translator
    from findpic.interpret import describe_altitude

    note = describe_altitude(-413.2, below=True)
    assert note is not None
    rendered = note.render(Translator("en"))
    assert "-413" not in rendered
    assert "413" in rendered
    assert "below" in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [(21.8535, "22"), (6.0, "6"), (6.4, "6.4"), (0.0446, "0.0"), (105.9, "106"), (None, None)],
)
def test_measured_rounds_to_the_precision_of_the_measurement(value, expected) -> None:
    assert measured(value) == expected
