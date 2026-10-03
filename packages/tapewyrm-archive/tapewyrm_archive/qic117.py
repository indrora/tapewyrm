"""What the QIC-117 report bytes stored in TWRF/TWTI headers mean.

A capture records the drive's raw report bytes (Report Drive Status, Drive
Configuration, Vendor ID, Tape Status; DESIGN.md §13.1) so it is
self-describing. Decoding them is needed both live, by ``tw`` talking to a
drive, and offline, by qiclib/qicsilver describing an image -- so the decoders
live here with the formats, which every package already depends on. Error
codes and the drive state machine stay in ``tapewyrm.qic117`` (tapewyrm-cli).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from tapewyrm_archive.types import TapeFormat

log = logging.getLogger(__name__)


def _bit(value: int, n: int) -> bool:
    return bool((value >> n) & 1)


@dataclass(frozen=True)
class DriveStatus:
    """Report Drive Status (cmd 6), 8 bits. Bits 1,5,6,7 valid only when ready."""

    ready: bool
    error: bool
    cartridge_present: bool
    write_protect: bool
    new_cartridge: bool
    referenced: bool
    at_bot: bool
    at_eot: bool
    raw: int = 0

    @classmethod
    def decode(cls, b: int) -> DriveStatus:
        return cls(
            ready=_bit(b, 0),
            error=_bit(b, 1),
            cartridge_present=_bit(b, 2),
            write_protect=_bit(b, 3),
            new_cartridge=_bit(b, 4),
            referenced=_bit(b, 5),
            at_bot=_bit(b, 6),
            at_eot=_bit(b, 7),
            raw=b,
        )


@dataclass(frozen=True)
class DriveConfig:
    """Report Drive Configuration (cmd 8), 8 bits.

    bits 3-4 rate (00=4M/250k, 01=2M, 10=500k, 11=1M); bit6 extra-length;
    bit7 QIC-80-mode.
    """

    rate_code: int
    extra_length: bool
    qic80_mode: bool
    raw: int = 0

    # rate code -> kbps. 00 is ambiguous (4 Mbps OR 250 kbit/s by drive type);
    # we surface 250 as the conservative default and flag the ambiguity.
    _RATE_KBPS = {0b00: 250, 0b01: 2000, 0b10: 500, 0b11: 1000}

    @classmethod
    def decode(cls, b: int) -> DriveConfig:
        return cls(
            rate_code=(b >> 3) & 0b11,
            extra_length=_bit(b, 6),
            qic80_mode=_bit(b, 7),
            raw=b,
        )

    @property
    def rate_kbps(self) -> int:
        return self._RATE_KBPS[self.rate_code]

    @property
    def rate_ambiguous(self) -> bool:
        return self.rate_code == 0b00


@dataclass(frozen=True)
class TapeStatus:
    """Report Tape Status (cmd 33), 8 bits: 0-3 format, 4-6 type, 7 wide."""

    format: TapeFormat
    tape_type: int
    wide: bool
    raw: int = 0

    @classmethod
    def decode(cls, b: int) -> TapeStatus:
        fmt_bits = b & 0x0F
        fmt = (
            TapeFormat(fmt_bits)
            if fmt_bits in TapeFormat._value2member_map_
            else TapeFormat.UNKNOWN
        )
        return cls(format=fmt, tape_type=(b >> 4) & 0b111, wide=_bit(b, 7), raw=b)


# QIC-117 Rev J "Assigned Vendor Make Codes (0-1023)" (p.24).
VENDOR_MAKES: dict[int, str] = {
    0: "Unassigned",
    1: "Alloy Computer Products",
    2: "3M",
    3: "Tandberg Data",
    4: "Colorado Memory Systems",
    5: "Archive/Conner",
    6: "Mountain/Summit Memory Systems",
    7: "Wangtek/Rexon/Tecmar",
    8: "Sony",
    9: "Cipher Data Products",
    10: "Irwin Magnetic Systems",
    11: "Braemar",
    12: "Verbatim",
    13: "Core International (Shipped Unassigned)",
    14: "Exabyte",
    15: "Teac",
    16: "Gigatek",
    17: "ComByte",
    18: "PERTEC Memories",
    19: "Aiwa",
    71: "Colorado Memory Systems",
    546: "Iomega Inc.",
}


# Vendor IDs that predate the make/model split and are reported as a bare word.
# Rev J lists Colorado as make "4 & 71", but 71 as a 10-bit make would need a
# word >= 71 << 6 = 4544; the bench Colorado Jumbo 350 reports exactly 0x0047.
# So 71 is Colorado's legacy whole-word ID, not a make field.
LEGACY_VENDOR_IDS: dict[int, str] = {71: "Colorado Memory Systems (legacy ID)"}


def decode_vendor_id(value: int) -> tuple[int, int, str]:
    """Split a Report Vendor ID word: bits 6-15 make, 0-5 model (Rev J p.15).

    Legacy whole-word IDs (see ``LEGACY_VENDOR_IDS``) come back as
    (value, 0, name) rather than being mis-split into a bogus make/model.
    """
    if value in LEGACY_VENDOR_IDS:
        log.debug("vendor id 0x%04x is a legacy whole-word id; not splitting make/model", value)
        return value, 0, LEGACY_VENDOR_IDS[value]
    make, model = value >> 6, value & 0x3F
    return make, model, VENDOR_MAKES.get(make, f"unknown make {make}")


# Report Tape Status bits 4-6 (Rev J Table 2c).
TAPE_TYPES: dict[int, str] = {
    0: "unknown",
    1: "205 ft or 425+ ft, 550 Oe",
    2: "307.5 ft 550 Oe (XL)",
    3: "variable length 550 Oe",
    4: "1100 ft 550 Oe",
    6: "variable length 900 Oe",
}
