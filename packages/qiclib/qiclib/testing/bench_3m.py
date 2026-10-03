"""REAL bytes off the 3M DC2120 "XIMAT" bench tape (2026-10-01).

Handwritten label "WIN3.11 Office Borland WP6.0"; capture
captures/3m-unknown-1/track-00.twrf (TWRF, drive tape status 0x22, config 0xd0).
Header and volume-table segments both RS-decoded clean. Format code 2 (a Rev K
fixed format), 28 x 150 segments, factory pre-formatted by 3M.
"""

from __future__ import annotations

# Header segment, sector 0, bytes 0-233 (234-1023 are zero).
HEADER_SECTOR0 = bytes.fromhex(
    "55aa55aa02000000010002006710376ed131d6057f32000096001c0695802020"
    "2020202020202020202020202020202020202020202020202020202020202020"
    "20202020202020202020376ed131030000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000d320000000000000376ed13101000200334d202020202051494338302d49"
    "20494f383046694036384950532056312e31312e323241204944352020203030"
    "3031202020202020202020202020202020202020202020202020202020202020"
    "20202020202020202020"
).ljust(1024, b"\x00")

# The fixed-format bad-sector map (32-bit mask per segment from offset 2048,
# see qiclib.volume.FIXED_BSM_OFFSET): the only two non-zero bytes in the rest
# of the header's data area. Segment 156 sector 21, segment 410 sector 9.
BSM_BYTES = {2674: 0x20, 3689: 0x02}


def header_data() -> bytes:
    """The header segment's 29-sector data area, as RS correction returns it."""
    data = bytearray(HEADER_SECTOR0.ljust(29 * 1024, b"\x00"))
    for offset, value in BSM_BYTES.items():
        data[offset] = value
    return bytes(data)


# Volume table segment, the only record (the rest of the segment is zero).
VTBL = bytes.fromhex(
    "5654424c0400850d000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000001d5fa53900014d544e000000"
    "0000000000000000000000000000000000000000000000000000000000e00000"
    "058e410a06164449534b315f564f4c31202020202020030081"
).ljust(128, b"\x00")
