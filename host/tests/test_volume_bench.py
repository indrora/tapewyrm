"""Regression tests on REAL bytes read off the bench tape (2026-09-30).

Colorado Jumbo 350, QIC-80 cartridge "jc", track 0, capture
captures/2026-09-30-colorado350-track0-cap1.raw (sha256 8de4ffd0...); every
sector decoded with good ID + data CRCs. Field meanings per QIC-80-MC Rev N
(docs/qic80n.pdf) sections 7.1 and 8.
"""

from tapewyrm.codec.volume import (
    _parse_bsm,
    _parse_vtbl_entry,
    decode_short_date,
    parse_header,
)
from tests.fixtures.builders import make_segment_from_sectors

# Header segment, sector 0, first 160 bytes (the remaining 864 bytes are zero).
HEADER = bytes.fromhex(
    "55aa55aa0500000001000200a31678abde39dbbade390000cf001c0995806a63"
    "2020202020202020202020202020202020202020202020202020202020202020"
    "2020202020202020202078abde39000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000462d000000000000e9da2637020000000000000000000000000000000000"
).ljust(1024, b"\x00")

# Volume table segment, first 128-byte VTBL record (a CMS backup).
VTBL = bytes.fromhex(
    "5654424c0300470a46696c65732066726f6d204469736b315f766f6c31202843"
    "3a292020202020202020202020202020202020202fb1de39250171000600434d"
    "5300000000150018150646000000000000000000000000000000000054490b00"
    "c6908f0700000000000020202020202020202020202020202020bf0081060000"
)


def test_header_fields_match_rev_n_offsets():
    vol, bsm = parse_header(make_segment_from_sectors(0, 0, 0, [HEADER]))
    assert vol.valid_signature and vol.format_code == 5
    assert (vol.header_seg, vol.dup_header_seg) == (0, 1)
    assert (vol.first_data_seg, vol.last_data_seg) == (2, 5795)
    assert (vol.segments_per_track, vol.tracks) == (207, 28)
    assert (vol.max_fsd, vol.max_ftk, vol.max_fsc) == (9, 149, 128)
    assert vol.tape_name == "jc"
    assert vol.segments_written == 11590 and vol.format_count == 2
    assert not vol.reformat_error
    # Offsets 130-141 used to be read as two "bad sectors" (map read from 128).
    assert not bsm.bad_lsns and not bsm.bad_segments


def test_header_dates_are_calendar_dates():
    vol, _ = parse_header(make_segment_from_sectors(0, 0, 0, [HEADER]))
    # 14-17 most recent format; 74-77 tape name written (same moment here).
    assert decode_short_date(vol.format_date) == (1998, 12, 23, 1, 55, 4)
    assert decode_short_date(vol.name_date) == (1998, 12, 23, 1, 55, 4)
    assert decode_short_date(vol.write_date) == (1998, 12, 23, 3, 0, 43)  # last write
    # Formatted twice: first 1997-08-07, again on 1998-12-23 just before the backup.
    assert decode_short_date(vol.initial_format_date) == (1997, 8, 7, 15, 40, 25)


def test_fixed_format_bsm_is_a_per_segment_bitmap_at_2048():
    # Format code 5: 32-bit mask per segment from offset 2048, bit k = sector k.
    # Verified on the bench tape: segment 129 = 0x800 (sector 11 excluded).
    data = bytearray(HEADER.ljust(29 * 1024, b"\x00"))
    data[2048 + 4 * 129 : 2048 + 4 * 130] = (0x800).to_bytes(4, "little")
    data[2048 + 4 * 7 : 2048 + 4 * 8] = (0xFFFFFFFF).to_bytes(4, "little")
    bsm = _parse_bsm(bytes(data), format_code=5)
    assert bsm.bad_lsns == {129 * 32 + 11}
    assert bsm.bad_segments == {7}


def test_apply_bsm_marks_excluded_slots():
    from tapewyrm.codec.volume import BadSectorMap, apply_bsm
    from tapewyrm.types import Segment

    segs = {(0, 129): Segment(tpt=0, tps=129, seg=129), (0, 5): Segment(tpt=0, tps=5, seg=5)}
    assert apply_bsm(segs, BadSectorMap(bad_lsns={129 * 32 + 11})) == 1
    assert segs[(0, 129)].excluded == {11} and not segs[(0, 5)].excluded


def test_vendor_specific_vtbl_entry():
    e = _parse_vtbl_entry(VTBL)
    assert e.signature == b"VTBL"
    assert (e.start_seg, e.end_seg) == (3, 2631)  # words at 4-7, not doublewords
    assert e.description == "Files from Disk1_vol1 (C:)"  # starts at offset 8
    assert decode_short_date(e.date) == (1998, 12, 23, 2, 19, 27)
    assert e.flags == 0x25 and e.vendor_specific and e.directory_last
    # Vendor-specific: Rev N defines nothing past byte 56.
    assert e.os_type is None and e.compressed is None and e.dir_section_size is None
