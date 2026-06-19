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
    "ERR_BROKEN_TAPE",
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
ERR_RESET_OCCURRED = 1  # benign: drive was reset; status re-reads cleanly
ERR_BROKEN_TAPE = 10  # FATAL: physical tape break — abort the sweep immediately

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
BENIGN_ERRORS: frozenset[int] = frozenset({ERR_NO_ERROR, ERR_RESET_OCCURRED})


def classify_error(code: int) -> bool:
    """Return True if the error code is **fatal** (abort the sweep).

    Broken-tape (10) is fatal; reset-occurred and unclassified codes are benign
    (DESIGN.md §6A.3).
    """
    return code in FATAL_ERRORS


# Alias with a clearer name at the call site.
is_fatal = classify_error
