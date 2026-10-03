"""Offline decode codec for Tapewyrm (DESIGN.md §6.4, §6A.5, §13.5).

Pure-Python, hardware-free decode stack: flux bytes -> intervals -> MFM sectors
-> placed sectors -> segments -> RS erasure decode -> volume byte streams ->
QIC-113 file sets. Every stage is a pure function over the foundation dataclasses
in ``tapewyrm.types`` and is fixture-testable with no drive attached.

The genuine hardware/bench seams (GW flux opcode bytes, the interval->bitstream
PLL) are isolated and marked ``TODO(bench)`` per DESIGN.md §13.6 item 1.
"""

from __future__ import annotations
