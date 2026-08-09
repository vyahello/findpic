"""Tests for reading a capture time out of a filename.

The negatives matter as much as the positives here. A function willing to guess
would put a confident wrong date on somebody's photograph, and a wrong date is
worse than no date — it looks like evidence.
"""

from __future__ import annotations

import datetime as dt

import pytest

from findpic.recover import (
    PRECISION_DAY,
    PRECISION_SECOND,
    timestamp_from_filename,
)

MOMENT = dt.datetime(2023, 8, 13, 14, 54, 35)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("IMG_20230813_145435.jpg", "android"),
        ("PXL_20230813_145435123.jpg", "android"),
        ("VID_20230813_145435.mp4", "android"),
        ("MVIMG_20230813_145435.jpg", "android"),
        ("Screenshot_20230813-145435.png", "screenshot"),
        ("Screenshot 2023-08-13 at 14.54.35.png", "macos_screenshot"),
        ("Screen Shot 2023-08-13 at 14.54.35.png", "macos_screenshot"),
        ("photo_2023-08-13_14-54-35.jpg", "telegram"),
        ("signal-2023-08-13-145435.jpg", "signal"),
        ("signal-2023-08-13-14-54-35.jpg", "signal"),
        ("WhatsApp Image 2023-08-13 at 14.54.35.jpeg", "whatsapp"),
        ("20230813_145435.jpg", "timestamped"),
        ("2023-08-13 14.54.35.jpg", "timestamped"),
    ],
)
def test_a_filename_that_carries_a_moment_gives_it_up(name: str, source: str) -> None:
    found = timestamp_from_filename(name)
    assert found is not None, name
    assert found.moment == MOMENT
    assert found.source == source
    assert found.precision == PRECISION_SECOND
    assert found.exif_value == "2023:08:13 14:54:35"


def test_a_date_only_name_is_reported_as_date_only() -> None:
    """WhatsApp's trailing digits are a counter, and must not become a time.

    Reading 0002 as a time of day would put an invented hour on the picture and
    present it with the same confidence as a real one.
    """
    found = timestamp_from_filename("IMG-20230813-WA0002.jpg")
    assert found is not None
    assert found.precision == PRECISION_DAY
    assert not found.is_exact
    assert found.moment.date() == MOMENT.date()
    assert (found.moment.hour, found.moment.minute) == (0, 0)


@pytest.mark.parametrize(
    "name",
    [
        "IMG_2781.JPG",  # Apple: a counter, never a date
        "IMG_1312.JPG",
        "DSC_0001.JPG",
        "33.JPG",
        "22.jpeg",
        "photo.jpg",
        "Snapchat-1691938475.jpg",  # an id that happens to be digits
        "IMG_20231345_145435.jpg",  # month 13, day 45
        "IMG_20230813_995435.jpg",  # hour 99
        "19700101_000000.jpg",  # predates the format
        "20991231_235959.jpg",  # dated well past tomorrow
        "1234567890.jpg",
    ],
)
def test_a_filename_without_a_real_moment_gives_nothing(name: str) -> None:
    assert timestamp_from_filename(name) is None


def test_an_apple_counter_is_never_read_as_a_date() -> None:
    """The single most important negative.

    Apple has never put a date in a filename. IMG_2781 is the 2781st picture,
    and any date derived from it would be pure invention — on exactly the files
    that most need an honest answer, because they have nothing else left.
    """
    for number in range(1000, 9999, 337):
        assert timestamp_from_filename(f"IMG_{number}.JPG") is None


def test_the_matched_text_is_reported_so_the_reader_can_check_it() -> None:
    found = timestamp_from_filename("IMG_20230813_145435.jpg")
    assert found is not None
    assert found.matched == "IMG_20230813_145435"


def test_a_leap_day_is_accepted_and_a_fake_one_is_not() -> None:
    assert timestamp_from_filename("20240229_120000.jpg") is not None
    assert timestamp_from_filename("20230229_120000.jpg") is None
