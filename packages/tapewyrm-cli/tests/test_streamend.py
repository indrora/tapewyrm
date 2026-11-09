"""StreamEndScanner: find GW's end-of-stream 00 token across chunk boundaries."""

import pytest

from tapewyrm.link.streamend import StreamEndScanner

# One of every token shape, none of them a terminator, several holding 00 or FF
# bytes that a naive "look for 00" scan would trip over.
TOKENS = [
    b"\x48",  # 1-byte interval
    b"\xfa\xff",  # 2-byte interval whose second byte is FF (data, not escape)
    bytes([0xFF, 0x01, 0x01, 0x03, 0x05, 0x07]),  # INDEX N28
    bytes([0xFF, 0x02, 0x01, 0x01, 0x01, 0x01]),  # SPACE N28 (dead time)
    bytes([0xFF, 0x02, 0x01, 0x01, 0x01, 0x01, 0xF9]),  # SPACE + long-interval tail
    bytes([0xFF, 0xF3, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00]),  # END marker, zeros
    bytes([0xFF, 0xF0, 0x00]),  # marker with an empty payload
]
BODY = b"".join(TOKENS)
STREAM = BODY + b"\x00"


def test_whole_stream_in_one_chunk():
    scanner = StreamEndScanner()
    assert scanner.feed(STREAM)
    assert scanner.end_offset == len(BODY)


@pytest.mark.parametrize("cut", range(1, len(STREAM)))
def test_every_two_way_split(cut):
    scanner = StreamEndScanner()
    assert not scanner.feed(STREAM[:cut]) or cut == len(STREAM)
    assert scanner.feed(STREAM[cut:])
    assert scanner.end_offset == len(BODY)


def test_byte_at_a_time():
    scanner = StreamEndScanner()
    results = [scanner.feed(bytes([b])) for b in STREAM]
    assert results == [False] * len(BODY) + [True]
    assert scanner.end_offset == len(BODY)


def test_body_without_terminator_never_ends():
    scanner = StreamEndScanner()
    assert not scanner.feed(BODY)
    assert not scanner.feed(b"")


def test_bytes_after_the_end_are_ignored():
    scanner = StreamEndScanner()
    assert scanner.feed(b"\x48\x00\x98\x09")
    assert scanner.end_offset == 1
    assert scanner.feed(b"\x48")  # stays ended
