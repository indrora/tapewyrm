"""REAL bytes off the "unk-co" bench tape (captured 2026-11-06, read 2026-11-07).

A QIC-3010 400 ft cartridge, factory pre-formatted "Full Format QIC3010_LONG
CMS - HP, Inc", tape name 0917962215, formatted 1996-09-17 and written
1997-10-24 (captures/unk-co, 26 tracks; the drive's tape status 0x64,
config 0x50). Format code 4 (QIC-3010-MC Rev H), 40 x 298 segments. One
volume, "Backup Misc Files 10/23/97", segments 3-7081, QIC-122, 386.3 MB.

What makes it a fixture: a CMS QIC-113 Rev F extended volume whose VTBL has
the vendor-specific bit CLEAR (flags 0x24, where the jc tape's are 0x25),
bytes 57-65 otherwise identical to jc's (``01 | 71 00 | 06 00 | "CMS\\0"``).
Matching CMS tapes on the vendor bit routed this one to the Basic-DOS
parser, which listed 186 nameless zero-byte files; the profiles match on
the 113 signature and "CMS" since. Only header and table metadata are
here: the tape is a stranger's backup and its listing stays out of the repo.
"""

from __future__ import annotations

# Header segment, sector 0, bytes 0-233 (234-1023 are zero). The real tape's
# bad-sector map (241 sectors, 48 segments, from offset 256 for format code
# 4) is not reproduced: nothing here reads past the format parameter record.
HEADER_SECTOR0 = bytes.fromhex(
    "55aa55aa04000000010002008f2e1245"
    "5d35e03e8f3700002a01280bfe803039"
    "31373936323231352000000000000000"
    "00000000000000000000000000000000"
    "0000000000000000000012455d350000"
    "00000000000000000000000000000000"
    "00000000000000000000000000000000"
    "00000000000000000000000000000000"
    "0000000000000000000012455d350100"
    "000046756c6c20466f726d6174202051"
    "4943333031305f4c4f4e472020202020"
    "20434d53202d2048502c20496e633131"
    "33422055533030303737343437205630"
    "332e3032204230303936342046303032"
    "39322054303430202020"
).ljust(1024, b"\x00")


def header_data() -> bytes:
    """The header segment's 29-sector data area, as RS correction returns it."""
    return HEADER_SECTOR0.ljust(29 * 1024, b"\x00")


# Volume table segment, the only record (the rest of the segment is zero).
# Decoded by the cms-qic113 profile: directory section 25,694 bytes (92-95),
# data section 386,322,749 (96-103), label empty, byte 124 = 0x81 (compressed,
# QIC-123 code 1), byte 125 = 7, cartridge 1 of the set.
VTBL = bytes.fromhex(
    "5654424c0300a91b4261636b7570204d"
    "6973632046696c65732031302f32332f"
    "39372020202020202020202020202020"
    "202020208a2a8f37240171000600434d"
    "53000000011400000000460100000000"
    "0000000000202020202020205e640000"
    "3dd10617000000000000000000000000"
    "0000000101ea07000000c00081070000"
)
