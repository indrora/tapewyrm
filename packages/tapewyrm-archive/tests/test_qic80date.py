"""The shared QIC-80 packed short date decoder (``tapewyrm_archive.qic80date``)."""

from __future__ import annotations

from tapewyrm_archive.qic80date import decode_short_date, format_short_date


def _pack(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    """Pack a calendar date (1-based month and day) the QIC-80 way."""
    rest = second + 60 * (minute + 60 * (hour + 24 * ((day - 1) + 31 * (month - 1))))
    return ((year - 1970) << 25) | rest


def test_round_trip_gives_calendar_month_and_day():
    assert decode_short_date(_pack(1998, 12, 23, 1, 55, 4)) == (1998, 12, 23, 1, 55, 4)


def test_zero_and_all_ones_are_undefined():
    assert decode_short_date(0) is None and decode_short_date(0xFFFFFFFF) is None
    assert format_short_date(0) is None


def test_format_is_iso_style():
    assert format_short_date(_pack(1999, 11, 21, 12, 30, 2)) == "1999-11-21 12:30:02"
