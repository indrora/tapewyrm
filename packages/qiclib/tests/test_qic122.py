"""QIC-122 decompression (docs/qic122b.pdf) and QIC-113 extents (docs/qic113g.pdf)."""

import struct

import pytest

from qiclib import qic122


def _bits_to_bytes(bits: str) -> bytes:
    bits += "0" * (-len(bits) % 8)
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))


def test_appendix_a_example():
    # QIC-122 Rev B, Appendix A: output byte stream and the input it encodes.
    stream = bytes.fromhex("20 90 88 38 1C 21 E2 5C 15 80")
    assert qic122.decompress(stream) == b"ABAAAAAACABABABA"


def test_long_lengths_and_11_bit_offsets():
    # raw 'x', raw 'y', then copy offset 2 (11-bit form), length 8+15+1 = 24.
    bits = "0" + f"{ord('x'):08b}" + "0" + f"{ord('y'):08b}"
    bits += "1" + "0" + f"{2:011b}" + "11" + "11" + "1111" + "0001"
    bits += "1" + "1" + "0000000"  # end marker
    assert qic122.decompress(_bits_to_bytes(bits)) == b"xy" + b"xy" * 12


def test_missing_end_marker_and_bad_offset_raise():
    with pytest.raises(qic122.Qic122Error):
        qic122.decompress(_bits_to_bytes("0" + "01000001"))  # no end marker
    with pytest.raises(qic122.Qic122Error):
        qic122.decompress(_bits_to_bytes("1" + "1" + "0000101" + "00"))  # offset 5, empty history


def test_decode_extent_frames_raw_and_null_fill():
    compressed = bytes.fromhex("20 90 88 38 1C 21 E2 5C 15 80")
    seg = struct.pack("<Q", 71_724)
    seg += struct.pack("<H", len(compressed)) + compressed
    seg += struct.pack("<H", 0x8000 | 5) + b"HELLO"  # hi bit = stored raw
    seg = seg.ljust(29 * 1024, b"\x00")  # < 18 bytes left is fill; zero size ends it too
    ext = qic122.decode_extent(seg)
    assert ext.uncompressed_offset == 71_724
    assert ext.frames == 2
    assert ext.data == b"ABAAAAAACABABABA" + b"HELLO"


def test_truncated_string_token_is_a_qic122_error_not_a_bare_valueerror():
    # raw 'A', then an 11-bit-offset token cut off by the end of the frame:
    # the length slice comes back empty and int('', 2) used to escape as a
    # plain ValueError, which twvl.extract does not catch.
    with pytest.raises(qic122.Qic122Error, match="truncated string token"):
        qic122.decompress(_bits_to_bytes("0" + "01000001" + "10"))


def test_decode_extent_four_byte_offset():
    # MTN tapes: a doubleword offset (profiles/volume/mtn.toml [extent]).
    seg = struct.pack("<I", 29_690) + struct.pack("<H", 0x8000 | 5) + b"HELLO"
    ext = qic122.decode_extent(seg.ljust(29 * 1024, b"\x00"), offset_bytes=4)
    assert (ext.uncompressed_offset, ext.data) == (29_690, b"HELLO")
    with pytest.raises(ValueError, match="4 or 8"):
        qic122.decode_extent(seg, offset_bytes=2)
