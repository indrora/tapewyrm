"""QIC-117 status helpers: error classification + decoder re-exports (DESIGN.md §6A.3).

The bit-level report decoders live elsewhere -- ``DriveStatus``,
``DriveConfig`` and ``TapeStatus`` in ``tapewyrm_archive.qic117`` (captures
record their raw bytes), ``ErrorCode`` in ``tapewyrm.types`` -- and are
re-exported here only for the convenience of callers in this package. **Do
not duplicate them.**

What is genuinely new here is the *error classification* table consulted by the
tape layer to decide whether to abort a sweep (DESIGN.md §6A.3): broken-tape (10)
is the canonical fatal case; reset-occurred is benign.
"""

from __future__ import annotations

import logging

# Re-export the decoders for convenience (single source: see the module docstring).
from tapewyrm_archive.qic117 import DriveConfig, DriveStatus, TapeStatus

from tapewyrm.types import ErrorCode

log = logging.getLogger(__name__)

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
    "error_name",
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
ERR_RESET_OCCURRED = ERR_POWER_ON_RESET  # old name; nothing uses it any more

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


def error_name(code: int) -> str:
    """Rev J name for an error code ("no error" for 0)."""
    if code == ERR_NO_ERROR:
        return "no error"
    return ERROR_NAMES.get(code, f"unknown/vendor error {code}")


def classify_error(code: int) -> bool:
    """Return True if the error code is **fatal** (abort the sweep).

    Broken-tape (10) is fatal; reset-occurred and unclassified codes are benign
    (DESIGN.md §6A.3).
    """
    return code in FATAL_ERRORS


# Alias with a clearer name at the call site.
is_fatal = classify_error
