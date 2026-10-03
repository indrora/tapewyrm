"""GW stream parser vs. an encoder that follows firmware/src/floppy.c exactly."""

import struct

from tapewyrm.codec import gwstream, mfm
from tapewyrm.link.protocol import EndReason, Marker


def _n28(x: int) -> bytes:
    return bytes(
        [
            (1 | (x << 1)) & 0xFF,
            (1 | (x >> 6)) & 0xFF,
            (1 | (x >> 13)) & 0xFF,
            (1 | (x >> 20)) & 0xFF,
        ]
    )


def _encode(ticks: int) -> bytes:
    """rdata_encode_flux() for one transition."""
    if ticks < 250:
        return bytes([ticks])
    high = (ticks - 250) // 255
    if high < 5:
        return bytes([250 + high, 1 + (ticks - 250) % 255])
    return b"\xff\x02" + _n28(ticks - 249) + bytes([249])


def _marker(kind: Marker, payload: bytes) -> bytes:
    return bytes([0xFF, kind.value, len(payload)]) + payload


def test_round_trip_with_markers_index_and_end_accounting():
    intervals = [3, 144, 249, 250, 288, 1524, 1525, 72_000, 5_000_000]
    flux = b"".join(_encode(t) for t in intervals)
    blob = _marker(Marker.SESSION_START, struct.pack("<HIHBH", 500, 72_000_000, 0, 0, 1))
    blob += flux[:2] + b"\xff\x01" + _n28(17) + flux[2:]  # INDEX mid-stream, no time
    end = struct.pack("<BIII", EndReason.EOT, len(intervals), len(flux), sum(flux) & 0xFFFFFFFF)
    blob += _marker(Marker.END, end) + b"\x00"

    ps = gwstream.parse(blob)
    assert ps.intervals == intervals
    assert ps.terminated and ps.verified
    assert ps.end is not None and ps.end.reason == EndReason.EOT
    assert ps.index_ticks == [3 + 144 + 17]  # offset from the previous transition
    assert ps.sample_clock_hz == 72_000_000


def test_dead_time_space_is_added_to_the_next_interval():
    blob = b"\xff\x02" + _n28(14_400) + bytes([100]) + b"\x00"  # long-gap SPACE then 100
    ps = gwstream.parse(blob)
    assert ps.intervals == [14_500]
    assert ps.data_bytes == 1  # dead-time SPACE isn't counted by the firmware


def test_gwstream_is_the_archive_parser_and_marker_codes_match_protocol():
    """One tokenizer: tw convert/dump and RawFluxCapture.verify read streams alike."""
    from tapewyrm_archive import twrf

    assert gwstream.parse is twrf.parse_body
    blob = _encode(759) + _marker(Marker.SEGMENT, struct.pack("<II", 1, 1)) + b"\x00"
    ps = gwstream.parse(blob)
    (seg,) = ps.markers
    assert seg.code == Marker.SEGMENT and seg.interval == 1 and seg.offset == 2
    assert twrf.flux_data_only(blob) == _encode(759) == b"\xfb\xff"


def _mfm_cells(data: bytes, prev: int = 0) -> str:
    out = []
    for byte in data:
        for i in range(7, -1, -1):
            bit = byte >> i & 1
            out.append("1" if (not bit and not prev) else "0")
            out.append(str(bit))
            prev = bit
    return "".join(out)


def test_bitcells_to_bytes_aligns_on_a1_sync():
    sync = "0100010010001001" * 3  # A1 A1 A1 with missing clocks
    payload = b"\xfe\x00\x00\x01\x03"
    cells = "1010" + _mfm_cells(b"\x00\x00") + sync + _mfm_cells(payload, prev=1)
    raw = bytes(int(c) for c in cells)
    assert mfm.bitcells_to_bytes(raw)[:8] == b"\xa1\xa1\xa1" + payload
