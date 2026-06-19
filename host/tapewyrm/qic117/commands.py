"""QIC-117 command table + N+2 argument encoder (DESIGN.md §13.1).

The complete Rev J command set (Tables 2a-2d) as frozen ``Cmd`` records keyed by
name, plus the pulse-train argument encoder. Arguments are emitted as a *second*
pulse train in **N+2 form** (value+2 pulses) so an argument can never collide
with the single-pulse Soft Reset (cmd 1) and zero is sendable; Soft Select (23)
is the exception, a literal 20 pulses (DESIGN.md §2.1, §13.1).

``Kind`` tags the dispatch class (the whole point of the table): REPORT / MODE /
MOTION / STREAM / SELECT / CONFIG / RESET / INTERNAL. The drive layer dispatches
on it (DESIGN.md §6A.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class Kind(Enum):
    """Command dispatch class (DESIGN.md §13.1: RPT/MOD/MOT/STREAM/SEL/CFG/RST/INT)."""

    REPORT = auto()
    MODE = auto()
    MOTION = auto()
    STREAM = auto()  # streaming-motion: Logical Forward — never wait-ready after it
    SELECT = auto()
    CONFIG = auto()
    RESET = auto()
    INTERNAL = auto()  # Report Next Bit — used inside the report clock, not dispatched


@dataclass(frozen=True)
class Cmd:
    """One QIC-117 command (DESIGN.md §13.1).

    ``non_intr`` = non-interruptible (Rev J flag *n*); ``high_speed`` = flag *h*.
    ``takes_arg`` means a following pulse train carries an operand. ``is_streaming``
    marks Logical Forward, after which we must NOT wait-ready/status (a status
    report would swallow segment 0 — DESIGN.md §2.1/§6A.3).
    """

    code: int
    kind: Kind
    non_intr: bool
    name: str
    takes_arg: bool = False
    high_speed: bool = False
    is_streaming: bool = False


# ---------------------------------------------------------------------------
# The full command table (Rev J Tables 2a/2b/2c/2d — DESIGN.md §13.1)
# Reserved: 19-20, 39.  Vendor-unique: 31, 40-45.
# ---------------------------------------------------------------------------

_CMDS: tuple[Cmd, ...] = (
    Cmd(1, Kind.RESET, True, "soft reset"),
    Cmd(2, Kind.INTERNAL, True, "report next bit"),
    Cmd(3, Kind.MOTION, True, "pause"),
    Cmd(4, Kind.MOTION, True, "micro step pause"),
    Cmd(5, Kind.CONFIG, False, "alternate command time-out"),
    Cmd(6, Kind.REPORT, False, "report drive status"),
    Cmd(7, Kind.REPORT, False, "report error code"),
    Cmd(8, Kind.REPORT, False, "report drive configuration"),
    Cmd(9, Kind.REPORT, False, "report rom version"),
    Cmd(10, Kind.STREAM, False, "logical forward", is_streaming=True),
    Cmd(11, Kind.MOTION, False, "physical reverse", high_speed=True),
    Cmd(12, Kind.MOTION, False, "physical forward", high_speed=True),
    Cmd(13, Kind.MOTION, False, "seek head to track", takes_arg=True),
    Cmd(14, Kind.MOTION, True, "seek load point"),
    Cmd(15, Kind.MODE, False, "enter format mode"),
    Cmd(16, Kind.MOTION, True, "write reference burst"),
    Cmd(17, Kind.MODE, False, "enter verify mode"),
    Cmd(18, Kind.MOTION, True, "stop tape"),
    # 19-20 reserved.
    Cmd(21, Kind.MOTION, True, "micro step head up"),
    Cmd(22, Kind.MOTION, True, "micro step head down"),
    Cmd(23, Kind.SELECT, False, "soft select", takes_arg=True),  # literal 20 pulses
    Cmd(24, Kind.SELECT, False, "soft deselect"),
    Cmd(25, Kind.MOTION, True, "skip n segs reverse", takes_arg=True),
    Cmd(26, Kind.MOTION, True, "skip n segs forward", takes_arg=True),
    Cmd(27, Kind.CONFIG, False, "select rate or format", takes_arg=True),
    Cmd(28, Kind.MODE, False, "enter diag mode 1", takes_arg=True),  # sent twice
    Cmd(29, Kind.MODE, False, "enter diag mode 2", takes_arg=True),  # sent twice
    Cmd(30, Kind.MODE, False, "enter primary mode"),
    Cmd(31, Kind.INTERNAL, False, "vendor unique 31"),  # vendor-unique
    Cmd(32, Kind.REPORT, False, "report vendor id"),
    Cmd(33, Kind.REPORT, False, "report tape status"),
    Cmd(34, Kind.MOTION, True, "skip n ext reverse", takes_arg=True),
    Cmd(35, Kind.MOTION, True, "skip n ext forward", takes_arg=True),
    Cmd(36, Kind.MOTION, True, "calibrate tape length"),
    Cmd(37, Kind.REPORT, False, "report format segments"),
    Cmd(38, Kind.CONFIG, False, "set n format segments", takes_arg=True),
    # 39 reserved.
    Cmd(40, Kind.INTERNAL, False, "vendor unique 40"),  # vendor-unique
    Cmd(41, Kind.INTERNAL, False, "vendor unique 41"),  # vendor-unique
    Cmd(42, Kind.INTERNAL, False, "vendor unique 42"),  # vendor-unique
    Cmd(43, Kind.INTERNAL, False, "vendor unique 43"),  # vendor-unique
    Cmd(44, Kind.INTERNAL, False, "vendor unique 44"),  # vendor-unique
    Cmd(45, Kind.INTERNAL, False, "vendor unique 45"),  # vendor-unique
    Cmd(46, Kind.SELECT, False, "phantom select", takes_arg=True),
    Cmd(47, Kind.SELECT, False, "phantom deselect"),
)

# Keyed by an UPPER_SNAKE name derived from the spec name, for ergonomic lookup.
TABLE: dict[str, Cmd] = {c.name.upper().replace(" ", "_").replace("-", "_"): c for c in _CMDS}

# By numeric code, for decoding transaction logs / associated-command fields.
BY_CODE: dict[int, Cmd] = {c.code: c for c in _CMDS}


# Convenience handles for the commands the drive/tape layers reference by name.
SOFT_RESET = TABLE["SOFT_RESET"]
REPORT_NEXT_BIT = TABLE["REPORT_NEXT_BIT"]
REPORT_DRIVE_STATUS = TABLE["REPORT_DRIVE_STATUS"]
REPORT_ERROR_CODE = TABLE["REPORT_ERROR_CODE"]
REPORT_DRIVE_CONFIGURATION = TABLE["REPORT_DRIVE_CONFIGURATION"]
LOGICAL_FORWARD = TABLE["LOGICAL_FORWARD"]
SEEK_HEAD_TO_TRACK = TABLE["SEEK_HEAD_TO_TRACK"]
SEEK_LOAD_POINT = TABLE["SEEK_LOAD_POINT"]
STOP_TAPE = TABLE["STOP_TAPE"]
SOFT_SELECT = TABLE["SOFT_SELECT"]
REPORT_TAPE_STATUS = TABLE["REPORT_TAPE_STATUS"]
REPORT_FORMAT_SEGMENTS = TABLE["REPORT_FORMAT_SEGMENTS"]
ENTER_PRIMARY_MODE = TABLE["ENTER_PRIMARY_MODE"]

# Soft Select sends a literal 20-pulse train, not an N+2 argument (DESIGN.md §2.1).
SOFT_SELECT_PULSES = 20


# ---------------------------------------------------------------------------
# N+2 argument encoder (DESIGN.md §2.1, §13.1)
# ---------------------------------------------------------------------------


def _plus2(value: int) -> int:
    if value < 0:
        raise ValueError(f"negative argument cannot be encoded: {value}")
    return value + 2


def encode_arg(cmd: Cmd, value: int) -> list[int]:
    """Encode a command's operand as pulse-train counts (DESIGN.md §13.1).

    Returns a list of pulse counts, one per following pulse train:

    * Soft Select (23): a single train of a **literal 20 pulses** (value ignored).
    * Seek Head to Track (13): one train of ``Track + 2``.
    * Skip N Segs Reverse/Forward (25/26): two nibble trains ``(N&15)+2, (N>>4)+2``.
    * Skip N Ext Reverse/Forward (34/35): three nibble trains, each ``nibble+2``.
    * Set N Format Segments (38): three nibble trains, each ``nibble+2``.
    * Anything else taking an arg (e.g. 27 rate/format, 46 unit): one ``value+2`` train.
    """
    if cmd.code == SOFT_SELECT.code:
        # Literal 20 pulses, regardless of value.
        return [SOFT_SELECT_PULSES]
    if not cmd.takes_arg:
        raise ValueError(f"command {cmd.name!r} (code {cmd.code}) takes no argument")
    if value < 0:
        raise ValueError(f"negative argument for {cmd.name!r}: {value}")

    # Two-nibble forms (8-bit value, low nibble then high nibble).
    if cmd.code in (TABLE["SKIP_N_SEGS_REVERSE"].code, TABLE["SKIP_N_SEGS_FORWARD"].code):
        return [_plus2(value & 0x0F), _plus2((value >> 4) & 0x0F)]

    # Three-nibble forms (12-bit value, low -> mid -> high nibble).
    if cmd.code in (
        TABLE["SKIP_N_EXT_REVERSE"].code,
        TABLE["SKIP_N_EXT_FORWARD"].code,
        TABLE["SET_N_FORMAT_SEGMENTS"].code,
    ):
        return [
            _plus2(value & 0x0F),
            _plus2((value >> 4) & 0x0F),
            _plus2((value >> 8) & 0x0F),
        ]

    # Plain single-train N+2 (Seek Head to Track 13, Select Rate/Format 27,
    # Phantom Select 46, Diag modes 28/29).
    return [_plus2(value)]
