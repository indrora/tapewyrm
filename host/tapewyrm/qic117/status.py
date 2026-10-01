"""QIC-117 status helpers: error classification + decoder re-exports (DESIGN.md §6A.3).

The bit-level report decoders (``DriveStatus``/``ErrorCode``/``DriveConfig``/
``TapeStatus``) LIVE in ``tapewyrm.types`` — they are re-exported here only for
the convenience of callers in this package. **Do not duplicate them.**

What is genuinely new here is the *error classification* table consulted by the
tape layer to decide whether to abort a sweep (DESIGN.md §6A.3): broken-tape (10)
is the canonical fatal case; reset-occurred is benign.
"""

from __future__ import annotations

# Re-export the decoders for convenience (they live in types.py — single source).
from tapewyrm.types import (
    DriveConfig,
    DriveStatus,
    ErrorCode,
    TapeStatus,
)

__all__ = [
    "DriveConfig",
    "DriveStatus",
    "ErrorCode",
    "TapeStatus",
    "classify_error",
    "is_fatal",
    "FATAL_ERRORS",
    "BENIGN_ERRORS",
    "ERR_NO_ERROR",
    "ERR_RESET_OCCURRED",
    "ERR_POWER_ON_RESET",
    "ERR_SOFTWARE_RESET",
    "ERR_WAKEUP_RESET",
    "ERR_BROKEN_TAPE",
    "ERROR_NAMES",
    "VENDOR_MAKES",
    "error_name",
    "decode_vendor_id",
    "TAPE_TYPES",
    "LEGACY_VENDOR_IDS",
]

# ---------------------------------------------------------------------------
# QIC-117 error codes (Rev J error table). Only the codes the recovery flow
# actually classifies are named; the rest default to benign (non-fatal), which
# is the safe choice for a read-only recovery sweep.
#
# The two load-bearing codes per DESIGN.md §6A.3:
#   * 10 = broken tape  -> canonical FATAL hard stop
#   * a reset-occurred / new-cartridge style code -> BENIGN
# ---------------------------------------------------------------------------

ERR_NO_ERROR = 0
ERR_BROKEN_TAPE = 10  # FATAL: physical tape break — abort the sweep immediately
# The reset family (benign: informational, cleared by reading the error code).
# NB: this used to say "reset occurred = 1", but Rev J code 1 is "Command
# Received while Drive Not Ready" -- the resets are 26, 27 and 41. The bench
# Colorado reports 26 after power-up and 41 after a phantom deselect/reselect.
ERR_POWER_ON_RESET = 26
ERR_SOFTWARE_RESET = 27
ERR_WAKEUP_RESET = 41
ERR_RESET_OCCURRED = ERR_POWER_ON_RESET  # kept for callers of the old name

# Codes that must abort the sweep. Broken-tape is the only one the design pins as
# the canonical fatal; other genuinely-unrecoverable hardware faults can be added
# here as the bench characterizes them.
#
# TODO(bench), DESIGN.md §9 item 3: expand FATAL_ERRORS / BENIGN_ERRORS against
# the real drive's Rev J error code emissions (vendor quirks) — characterize on
# the bench and fold into the DriveProfile/error table.
FATAL_ERRORS: frozenset[int] = frozenset({ERR_BROKEN_TAPE})

# Codes explicitly known to be benign (recoverable / informational). Everything
# not in FATAL_ERRORS is treated as benign by default; this set documents the
# intent and lets callers distinguish "known-benign" from "unclassified".
BENIGN_ERRORS: frozenset[int] = frozenset(
    {ERR_NO_ERROR, ERR_POWER_ON_RESET, ERR_SOFTWARE_RESET, ERR_WAKEUP_RESET}
)

# QIC-117 Rev J "Sequential Error Code List" (p.23), verbatim. (obs) = obsolete.
ERROR_NAMES: dict[int, str] = {
    1: "Command Received while Drive Not Ready",
    2: "Cartridge Not Present or Removed",
    3: "Motor Speed Error (not within 1%)",
    4: "Motor Speed Fault (jammed, or gross speed error)",
    5: "Cartridge Write Protected",
    6: "Undefined or Reserved Command Code",
    7: "Illegal Track Address Specified for Seek",
    8: "Illegal Command in Report Subcontext",
    9: "Illegal Entry into a Diagnostic Mode",
    10: "Broken Tape Detected (based on hole sensor)",
    11: "Warning -- Read Gain Setting Error",
    12: "Command Received While Error Status Pending (obs)",
    13: "Command Received While New Cartridge Pending",
    14: "Command Illegal or Undefined in Primary Mode",
    15: "Command Illegal or Undefined in Format Mode",
    16: "Command Illegal or Undefined in Verify Mode",
    17: "Logical Forward Not at Logical BOT or no Format Segments in Format Mode",
    18: "Logical EOT Before All Segments Generated",
    19: "Command Illegal When Cartridge Not Referenced",
    20: "Self-Diagnostic Failed (cannot be cleared)",
    21: "Warning EEPROM Not Initialized, Defaults Set",
    22: "EEPROM Corrupted or Hardware Failure",
    23: "Motion Time-out Error",
    24: "Data Segment Too Long -- Logical Forward or Pause",
    25: "Transmit Overrun (obs)",
    26: "Power On Reset Occurred",
    27: "Software Reset Occurred",
    28: "Diagnostic Mode 1 Error",
    29: "Diagnostic Mode 2 Error",
    30: "Command Received During Non-Interruptible Process",
    31: "Rate or Format Selection Error",
    32: "Illegal Command While in High Speed Mode",
    33: "Illegal Seek Segment Value",
    34: "Invalid Media",
    35: "Head Positioning Failure",
    36: "Write Reference Burst Failure",
    37: "Prom Code Missing",
    38: "Invalid Format",
    39: "EOT/BOT System Failure",
    40: "Prom A Checksum Error",
    41: "Drive Wakeup Reset Occurred",
    42: "Prom B Checksum Error",
    43: "Illegal Entry into Format Mode",
}

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


def error_name(code: int) -> str:
    """Rev J name for an error code ("no error" for 0)."""
    if code == ERR_NO_ERROR:
        return "no error"
    return ERROR_NAMES.get(code, f"unknown/vendor error {code}")


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


def classify_error(code: int) -> bool:
    """Return True if the error code is **fatal** (abort the sweep).

    Broken-tape (10) is fatal; reset-occurred and unclassified codes are benign
    (DESIGN.md §6A.3).
    """
    return code in FATAL_ERRORS


# Alias with a clearer name at the call site.
is_fatal = classify_error
