"""The QIC-80 packed "short date": the one decoder every package uses.

QIC-80 stamps dates as one 32-bit word: the header segment's format, write,
name and first-format dates (TWS-2 section 4.3 keeps them raw in a TWTI's
``qic80_header``) and each volume table entry's date (TWS-3 section 3.2).
Decoding them is needed by ``tapewyrm_archive.inspect`` (``tw inspect``) and
by qiclib (volume tables, ``qicsilver identify``), and the archive is the
package both already depend on (STYLE.md section 2), so it lives here.
``qiclib.volume`` re-exports :func:`decode_short_date` under its old name.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Both mean "no date was ever written" rather than 1970-01-01 00:00:00.
_UNDEFINED = (0, 0xFFFFFFFF)


def decode_short_date(packed: int) -> tuple[int, int, int, int, int, int] | None:
    """Decode a packed short date/time into (year, month, day, hour, min, sec).

    Encoding (QIC-80-MC Rev N section 7.1): bits 31..25 = year - 1970;
    bits 24..0 = ``sc + 60*(mn + 60*(hr + 24*(dy + 31*mo)))`` with MO 0-11 and
    DY 0-30. We return a calendar month 1-12 and day 1-31 (this used to leak the
    0-based values, so the bench tape read as "11/22" instead of 23 December).
    ``0`` and all-ones are treated as undefined -> None.
    """
    if packed in _UNDEFINED:
        log.debug("short date 0x%08x is undefined; returning None", packed)
        return None
    year = (packed >> 25) & 0x7F
    rest = packed & 0x01FFFFFF
    rest, sc = divmod(rest, 60)
    rest, mn = divmod(rest, 60)
    rest, hr = divmod(rest, 24)
    mo, dy = divmod(rest, 31)
    return (1970 + year, mo + 1, dy + 1, hr, mn, sc)


def format_short_date(packed: int) -> str | None:
    """A packed short date as ``YYYY-MM-DD HH:MM:SS``, or None when undefined."""
    when = decode_short_date(packed)
    if when is None:
        return None
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*when)
