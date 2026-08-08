"""Tests for the parsing and formatting helpers."""

from __future__ import annotations

from findpic.util import (
    parse_exif_datetime,
    same_moment,
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
