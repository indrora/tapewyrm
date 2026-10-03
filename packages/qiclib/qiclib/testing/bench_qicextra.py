"""REAL bytes off the Verbatim MC3020EX "QIC-Extra" bench tape (2026-10-03).

Label "QIC-Extra", 1.6 GB: an extended-length QIC-3020 cartridge whose shell
sticks out past a standard minicartridge. Factory formatted 1999-11-21, stamp
"FMTJ", never written since. Captured on a Colorado drive (Report Tape Status
0x63: QIC-3020, type 6 "variable length 900 Oe", not wide; config 0xd8:
1000 kbps, extra-length) and converted to TWTI; the header segment decoded
clean. Format code 4, 40 x 1475 segments, 35 bad sectors + 98 bad segments.
The volume table segment is all zero (no volumes).
"""

from __future__ import annotations

# Header segment data, bytes 0-654: the format parameter record (0-255) and
# the bad-sector map's 3-byte LSN entries up to their terminator. The rest of
# the 29-sector data area is zero.
HEADER_HEAD = bytes.fromhex(
    "55aa55aa040000000100020077e6cabeb33bcabeb33b0000c3052839fe802020"
    "2020202020202020202020202020202020202020202020202020202020202020"
    "20202020202020202020cabeb33b000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000f3cc010000000000cabeb33b01000000464d544a20202020202020202020"
    "2020202020202020202020202020202020202020202020202020202020202020"
    "2020202020202020202020202020202020202020202020202020202020202020"
    "2020202020202020202000000000000000000000000000000000000000000000"
    "bf15003633005d3300179a00bbca0029dd000162816d86016e860191e2014d09"
    "02ded1021cf702e19983019a83219a83419a8371ac03c15184e1518401528421"
    "5284e16784156804528304c78404a10a85c10a85e10a85010b8581c285a1c285"
    "c1c285e1c28501d8058323065d74067d7406617b86817b86a17b86c17b865d81"
    "06413387613387813387a1338739e40721ec8741ec8761ec8781ec8712640801"
    "a48821a48841a48861a488d8ba088e3909e15c89015d89215d89415d89028909"
    "c1148ae1148a01158a21158aa1cd8ac1cd8ae1cd8a01ce8a81858ba1858bc185"
    "8be1858b613e8c813e8ca13e8cc13e8c41f68c61f68c81f68ca1f68c21af8d41"
    "af8d61af8d81af8da5af0d01678e21678e41678e61678ee11f8f01208f21208f"
    "41208fc1d78fe1d78f01d88f21d88fa19090c19090e19090019190609e108148"
    "91a14891c14891e14891610192810192a10192c101920c051241b99261b99281"
    "b992a1b992217293417293617293817293127313012a94212a94412a94612a94"
    "4c3115a79c1639631a6c021b39731c"
)


def header_data() -> bytes:
    """The header segment's 29-sector data area, as RS correction returns it."""
    return HEADER_HEAD.ljust(29 * 1024, b"\x00")


# The capture's TWRF header fields that identify reads (its drive reports):
# tape status 0x63, drive config 0xd8, vendor id 4550 (Colorado Memory Systems).
DRIVE = {"tape_status": 0x63, "drive_config": 0xD8, "drive_vendor_id": 4550}
