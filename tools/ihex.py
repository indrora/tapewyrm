#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pure-Python Intel HEX utilities — merge + ELF->HEX, so the package build
needs no `srecord`/`srec_cat`.

`merge()` concatenates several Intel HEX images into one (each placed at the
addresses its own records specify), matching what GW's build does with
`srec_cat a -intel b -intel -o out -intel`: it drops every input's EOF record,
keeps the rest in order, and appends a single trailing EOF. Records carry their
own (possibly extended-linear) addresses, so order-preserving concatenation is
sufficient for a flashable image (bootloader region + application region).
"""

from __future__ import annotations

import sys
from pathlib import Path

EOF_RECORD = ":00000001FF"


def _checksum_ok(line: str) -> bool:
    raw = bytes.fromhex(line[1:])
    return (sum(raw) & 0xFF) == 0


def _validate(path: Path, lines: list[str]) -> None:
    for n, line in enumerate(lines, 1):
        if not line.startswith(":"):
            raise ValueError(f"{path}:{n}: not an Intel HEX record: {line!r}")
        if not _checksum_ok(line):
            raise ValueError(f"{path}:{n}: bad Intel HEX checksum: {line!r}")


def merge(inputs: list[str | Path], output: str | Path) -> Path:
    """Merge Intel HEX files in `inputs` into `output`. Returns the output path."""
    out_lines: list[str] = []
    for path in inputs:
        p = Path(path)
        lines = [ln.strip() for ln in p.read_text().splitlines() if ln.strip()]
        _validate(p, lines)
        # Drop this file's EOF record(s); a single EOF is appended at the end.
        out_lines.extend(ln for ln in lines if ln.upper() != EOF_RECORD)
    out_lines.append(EOF_RECORD)
    outp = Path(output)
    outp.write_text("\n".join(out_lines) + "\n")
    return outp


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: ihex.py <out.hex> <in1.hex> [in2.hex ...]", file=sys.stderr)
        raise SystemExit(2)
    merge(sys.argv[2:], sys.argv[1])
    print(f"wrote {sys.argv[1]} ({len(sys.argv) - 2} inputs merged)")
