"""GF(256) Reed-Solomon erasure decoder, redundancy 3 (DESIGN.md §13.2, §2.3).

The only nontrivial algorithm in qiclib; everything else is parsing.

Field
-----
Primitive polynomial ``f(x) = x^8 + x^7 + x^2 + x + 1`` -> reduction modulus
``0x187``; primitive element ``alpha = 0x02``. We build doubled ``exp[0..510]``
and ``log[0..255]`` tables and assert ``exp[105] == 0xC0`` (the ``r^105`` constant
from the generator) — a sanity check that fails loudly if the field/primitive is
ever mis-typed.

Generator / syndromes
---------------------
``g(x) = x^3 + 0xC0*x^2 + 0xC0*x + 1``. Its three roots are the syndrome
evaluation points; we find them once by search (degree 3 => exactly 3 roots).
They come out as ``{0x01, 0x02, 0xC3}``.

Codeword convention (GROUND TRUTH, DESIGN.md §13.2)
---------------------------------------------------
A segment's 32 sectors form 1024 independent codewords (one per byte column),
written **row by row**: codeword position ``i`` is the byte from sector/row ``i``.
Rows 0..28 are data, rows 29..31 are parity. The clean test column is
``rows 0..28 = 0x01..0x1D`` (value = row + 1) with parity rows 29,30,31 =
``0x5D 0xFF 0xA3``. With this convention:

  * syndromes of that clean codeword are all zero, and
  * erasing any <=3 of the 32 positions recovers it exactly.

(Both verified exhaustively against the vector.)

Syndrome ``S_j = sum_i recv[i] * root_j^i`` (position power = row index ``i``).
For a known erasure set ``E`` (|E| <= 3) we solve the |E|x|E| linear system
``sum_{e in E} root_j^pos(e) * X_e = S_j`` over GF(256) and write the solved
symbols back. This is erasure-only correction: the bad positions are known from
the per-sector CRC (DESIGN.md §2.3), giving the full redundancy-3 budget.

Excluded-sector repack (DESIGN.md §2.3, §7.3)
---------------------------------------------
BSM-excluded sectors are physically skipped on tape: the codeword is formed only
from the **non-excluded** sectors, parity occupying the last 3 of them, so the
codeword length is ``N = 32 - excluded_count`` (32 with nothing excluded). The
decoder packs the present non-excluded sectors down, decodes, then unpacks.
"""

from __future__ import annotations

import functools
import logging
from enum import StrEnum

from qiclib.types import Segment, SegmentResult, SegmentStatus

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GF(256) field tables
# ---------------------------------------------------------------------------

#: Reduction modulus from f(x) = x^8 + x^7 + x^2 + x + 1 (DESIGN.md §13.2).
GF_MODULUS = 0x187
#: Primitive element alpha.
GF_PRIMITIVE = 0x02


def _build_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    v = 1
    for i in range(255):
        exp[i] = v
        log[v] = i
        v <<= 1
        if v & 0x100:
            v ^= GF_MODULUS
    # Doubled table avoids a modulo in multiply.
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


EXP, LOG = _build_tables()
# Fails loudly if the field/primitive is ever wrong (DESIGN.md §13.2).
assert EXP[105] == 0xC0, "GF(256) field mis-built: r^105 != 0xC0"


def gmul(a: int, b: int) -> int:
    """GF(256) multiply."""
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def gdiv(a: int, b: int) -> int:
    """GF(256) divide (b != 0)."""
    if b == 0:
        log.debug("gdiv(%#04x, 0): divisor is zero; raising", a)
        raise ZeroDivisionError("GF(256) divide by zero")
    if a == 0:
        return 0
    return EXP[LOG[a] - LOG[b] + 255]


def ginv(a: int) -> int:
    """GF(256) multiplicative inverse (a != 0)."""
    if a == 0:
        log.debug("ginv(0): no inverse of zero; raising")
        raise ZeroDivisionError("GF(256) inverse of zero")
    return EXP[(255 - LOG[a]) % 255]


# ---------------------------------------------------------------------------
# Generator + syndrome roots
# ---------------------------------------------------------------------------

#: g(x) = x^3 + 0xC0 x^2 + 0xC0 x + 1, coefficients high-degree first.
GENERATOR = (1, 0xC0, 0xC0, 1)


def _g_eval(x: int) -> int:
    r = 0
    for c in GENERATOR:
        r = gmul(r, x) ^ c
    return r


def _find_roots() -> list[int]:
    roots = [e for e in range(1, 256) if _g_eval(e) == 0]
    if len(roots) != 3:
        log.debug("generator %r has %d roots %r, expected 3; raising", GENERATOR, len(roots), roots)
        raise RuntimeError(f"generator must have exactly 3 roots, found {len(roots)}")
    return roots


#: The 3 syndrome evaluation points (roots of g): {0x01, 0x02, 0xC3}.
ROOTS = _find_roots()
ROOT_LOG = [LOG[r] for r in ROOTS]

#: Total parity symbols (redundancy).
REDUNDANCY = 3


# ---------------------------------------------------------------------------
# Core single-codeword erasure decode
# ---------------------------------------------------------------------------


def syndromes(recv: list[int], length: int) -> list[int]:
    """Compute the 3 syndromes S_j = sum_i recv[i] * root_j^i over positions 0..length-1."""
    out: list[int] = []
    for rlog in ROOT_LOG:
        s = 0
        for i in range(length):
            ci = recv[i]
            if ci:
                # ci * root^i, with root^i = exp[(log(root)*i) mod 255].
                s ^= EXP[LOG[ci] + (rlog * i) % 255]
        out.append(s)
    return out


def _solve_linear(matrix: list[list[int]], rhs: list[int]) -> list[int]:
    """Solve a square linear system over GF(256) by Gaussian elimination."""
    m = len(rhs)
    aug = [matrix[r][:] + [rhs[r]] for r in range(m)]
    for col in range(m):
        pivot = next((r for r in range(col, m) if aug[r][col] != 0), None)
        if pivot is None:
            log.debug("erasure system %dx%d singular at column %d; raising", m, m, col)
            raise ValueError("singular erasure system")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        inv = ginv(aug[col][col])
        aug[col] = [gmul(x, inv) for x in aug[col]]
        for r in range(m):
            if r != col and aug[r][col] != 0:
                f = aug[r][col]
                aug[r] = [aug[r][k] ^ gmul(f, aug[col][k]) for k in range(m + 1)]
    return [aug[r][m] for r in range(m)]


def correct_codeword(recv: list[int], erasures: list[int], length: int) -> list[int]:
    """Recover up to 3 erased symbols of one length-``length`` codeword.

    ``recv`` has the erased positions set to 0; ``erasures`` lists those
    positions. Returns a new list with the erased symbols solved in place.
    Raises ``ValueError`` if there are more than 3 erasures (caller's contract:
    that case is the segment-level UNCORRECTABLE outcome).
    """
    if len(erasures) > REDUNDANCY:
        log.debug("%d erasures > redundancy %d; raising", len(erasures), REDUNDANCY)
        raise ValueError("too many erasures for redundancy-3 code")
    if not erasures:
        return recv[:]
    s = syndromes(recv, length)
    m = len(erasures)
    # For each of the first m syndrome equations: sum_e root_j^pos(e) * X_e = S_j.
    matrix = [[EXP[(ROOT_LOG[j] * erasures[e]) % 255] for e in range(m)] for j in range(m)]
    rhs = [s[j] for j in range(m)]
    solved = _solve_linear(matrix, rhs)
    out = recv[:]
    for k, pos in enumerate(erasures):
        out[pos] = solved[k]
    return out


# ---------------------------------------------------------------------------
# Segment-level decode (with excluded-sector repack)
# ---------------------------------------------------------------------------


def _participating(seg: Segment) -> list[int]:
    """Slot indices that take part in the codeword: non-excluded, in slot order.

    BSM-excluded sectors are physically skipped on tape, so they are dropped from
    the codeword entirely (DESIGN.md §2.3). The remaining ``N = 32 - excluded``
    slots form the codeword; parity occupies its last 3.
    """
    return [i for i in range(Segment.SECTORS) if i not in seg.excluded]


def erasure_positions(seg: Segment) -> list[int]:
    """Codeword positions (indices into the participating list) that are erased.

    A participating slot is erased if it is missing, its data CRC failed, or it
    is a deleted-data (format-time bad) block (DESIGN.md §2.3, §6.4 step 3).
    """
    participating = _participating(seg)
    erased: list[int] = []
    for pos, slot in enumerate(participating):
        sec = seg.sectors[slot]
        if sec is None or not sec.data_crc_ok or sec.deleted:
            erased.append(pos)
    return erased


def correct(seg: Segment) -> SegmentResult:
    """RS erasure-decode a segment, returning a :class:`SegmentResult`.

    * Erasure positions come from the per-sector CRC mask (DESIGN.md §2.3).
    * Codeword length ``N = 32 - excluded`` (the excluded-sector repack).
    * > 3 erasures -> UNCORRECTABLE (partial data kept, no correction attempted).
    * No erasures -> CLEAN, the data rows returned as read: no column decode
      (the comment in the body says why, and when it still runs).
    * Otherwise every byte column is solved over GF(256) and the segment rebuilt.

    The returned ``data`` is the concatenation of the 29 **data** sectors
    (rows 0..28) of the participating codeword, in order — 29 * 1024 bytes when
    the segment is whole. (Segment assembly / data extraction with excluded-row
    accounting lives in :mod:`qiclib.segment`; this returns the raw
    participating-data bytes so both layers agree on the column math.)
    """
    participating = _participating(seg)
    n = len(participating)
    erased = erasure_positions(seg)
    erasure_count = len(erased)

    # If nothing was ever placed, the segment is MISSING.
    if all(seg.sectors[i] is None for i in range(Segment.SECTORS)):
        log.debug(
            "segment %d (tpt %d tps %d): no sectors placed; MISSING", seg.seg, seg.tpt, seg.tps
        )
        return SegmentResult(status=SegmentStatus.MISSING, erasure_count=0)

    if erasure_count > REDUNDANCY:
        # Keep whatever clean data we have; do not attempt correction.
        log.debug(
            "segment %d: %d erasures at positions %r > redundancy %d (N=%d); "
            "UNCORRECTABLE, keeping partial data",
            seg.seg,
            erasure_count,
            erased,
            REDUNDANCY,
            n,
        )
        data = _extract_participating_data(seg, participating, n)
        return SegmentResult(
            status=SegmentStatus.UNCORRECTABLE,
            corrected_count=0,
            erasure_count=erasure_count,
            data=data,
        )

    width = _column_width(seg, participating)

    # No erasures: there is nothing to solve, and the column round trip below
    # would be an identity -- gather every row, correct nothing, scatter the
    # same bytes back -- costing 1.9 ms a segment in pure Python (1024 columns
    # by 32 rows, built twice). On a healthy tape that is nearly every
    # segment: the 40-track bench dump has 11,742 clean segments of 11,800,
    # and they were the whole 21-second "correcting segments" pass of
    # `tw convert`. The data rows concatenated are exactly what the column
    # path extracts (rows 0..N-4 of every column, row-major), at 7 us.
    #
    # The one input the two paths treat differently is a sector whose bytes
    # are not ``width`` long: the column path zero-fills that row (and writes
    # the zeros back into the sector), where the concatenation would pass the
    # odd bytes through. Such a segment keeps taking the column path, so the
    # output is the same whichever path runs.
    if erasure_count == 0 and _rows_are_whole(seg, participating, width):
        log.debug("segment %d: no erasures, N=%d; data rows taken as read", seg.seg, n)
        return SegmentResult(
            status=SegmentStatus.CLEAN,
            corrected_count=0,
            erasure_count=0,
            data=_extract_participating_data(seg, participating, n),
        )

    # Build the N x 1024 received matrix (erased rows are zeroed) and decode columns.
    log.debug(
        "segment %d: RS-decoding %d columns, N=%d (%d excluded), erasures at %r",
        seg.seg,
        width,
        n,
        len(seg.excluded),
        erased,
    )
    columns_recv = _gather_columns(seg, participating, n, width, erased)
    if erasure_count:
        for c in range(width):
            col = columns_recv[c]
            columns_recv[c] = correct_codeword(col, erased, n)

    # Write corrected symbols back into the sectors that exist, so downstream
    # code sees a repaired segment.
    _scatter_columns(seg, participating, columns_recv, width)

    # The data comes straight from the solved columns, NOT from re-reading the
    # sectors: a sector that was never read (None) has nowhere to be written
    # back to, and its rebuilt bytes used to be dropped -- the extraction then
    # emitted zeros for it.
    data_rows = max(0, n - REDUNDANCY)
    data = bytes(columns_recv[c][r] for r in range(data_rows) for c in range(width))
    status = SegmentStatus.CLEAN if erasure_count == 0 else SegmentStatus.CORRECTED
    return SegmentResult(
        status=status,
        corrected_count=erasure_count,
        erasure_count=erasure_count,
        data=data,
    )


# ---------------------------------------------------------------------------
# Column gather / scatter helpers
# ---------------------------------------------------------------------------


def _column_width(seg: Segment, participating: list[int]) -> int:
    """Byte width of the sectors (normally 1024)."""
    for i in participating:
        sec = seg.sectors[i]
        if sec is not None and sec.data:
            return len(sec.data)
    log.debug("no participating sector has data; assuming column width 1024")
    return 1024


def _rows_are_whole(seg: Segment, participating: list[int], width: int) -> bool:
    """True when every participating sector was read and holds exactly ``width`` bytes.

    The no-erasure fast path in :func:`correct` relies on this: with no
    erasures every participating sector exists with a good CRC, so only the
    length is in question. The MFM decoder always hands over 1024 bytes, but
    a fixture or an image need not, and :func:`_gather_columns` treats a row
    of the wrong length as unreadable (zero-filled), which the fast path
    would not.
    """
    for slot in participating:
        sec = seg.sectors[slot]
        if sec is None or len(sec.data) != width:
            log.debug(
                "segment %d slot %d holds %s bytes, not %d; taking the column path",
                seg.seg,
                slot,
                None if sec is None else len(sec.data),
                width,
            )
            return False
    return True


def _gather_columns(
    seg: Segment, participating: list[int], n: int, width: int, erased: list[int]
) -> list[list[int]]:
    """Return ``width`` codewords, each length ``n`` (row = participating index).

    Erased rows MUST be zero: :func:`correct_codeword` computes syndromes on the
    assumption that erased symbols are 0. Passing a CRC-failed sector's corrupt
    bytes through (as this used to) makes every correction of it wrong --
    found on the bench tape's segment 94, which decompressed 53 bytes short.
    """
    erased_set = set(erased)
    rows: list[bytes] = []
    for pos, slot in enumerate(participating):
        sec = seg.sectors[slot]
        if pos in erased_set:
            rows.append(bytes(width))
        elif sec is not None and sec.data and len(sec.data) == width:
            rows.append(sec.data)
        else:
            log.debug(
                "row %d (slot %d) not erased but has no %d-byte data (len %s); zero-filling",
                pos,
                slot,
                width,
                None if sec is None else len(sec.data),
            )
            rows.append(bytes(width))
    columns: list[list[int]] = []
    for c in range(width):
        columns.append([rows[r][c] for r in range(n)])
    return columns


def _scatter_columns(
    seg: Segment, participating: list[int], columns: list[list[int]], width: int
) -> None:
    """Write decoded columns back into the segment's sector data (in place)."""
    n = len(participating)
    for r in range(n):
        slot = participating[r]
        buf = bytearray(width)
        for c in range(width):
            buf[c] = columns[c][r]
        sec = seg.sectors[slot]
        if sec is None:
            log.debug("slot %d never read; solved row %d has nowhere to scatter, skipping", slot, r)
            continue
        sec.data = bytes(buf)
        # A solved/clean row is, after correction, CRC-consistent by construction.
        sec.data_crc_ok = True


def _extract_participating_data(seg: Segment, participating: list[int], n: int) -> bytes:
    """Concatenate the data sectors (all but the last 3 parity rows) in order.

    For a whole segment this is rows 0..28 = 29 * 1024 bytes. With excluded
    sectors the participating codeword is shorter and parity is still its last 3
    rows, so the data rows are ``participating[:-3]``.
    """
    data_rows = participating[: max(0, n - REDUNDANCY)]
    out = bytearray()
    for slot in data_rows:
        sec = seg.sectors[slot]
        if sec is not None and sec.data:
            out.extend(sec.data)
        else:
            log.debug(
                "segment %d slot %d has no data; zero-filling in partial extract", seg.seg, slot
            )
            out.extend(bytes(_column_width(seg, participating)))
    return bytes(out)


# ---------------------------------------------------------------------------
# The spare-parity check (what the erasure solve leaves unspent)
# ---------------------------------------------------------------------------
#
# The code has 3 parity rows, so 3 syndrome equations. ``correct_codeword``
# solves e erasures from the first e of them and never looks at the other
# 3 - e: with fewer than 3 erasures the codeword is over-determined, and the
# left-over equations say whether the rows taken as good agree with each
# other. That is the only check this layer has on a sector whose CRC passed:
# CRC-16 misses about 1 burst in 65,536, and a sector the decoder trusted
# wrongly makes the solve scatter garbage into the erased rows. ``correct``
# does not run the check (it would cost a pass over every clean segment);
# :func:`verify` does, and is cheap enough to run on all of them.
#
# Speed: a column at a time in Python is ~100 GF operations x 1024 columns
# per segment (tens of ms). But the syndromes are linear in whole rows:
# S_j = XOR_i row_i * alpha^(r_j * i), and multiplying a 1024-byte row by a
# constant is one ``bytes.translate`` through a 256-byte table, XOR of rows
# one big-int XOR. A clean segment's check is 96 of each: ~100 us.


class Parity(StrEnum):
    """What the spare parity of a segment says (:func:`verify`)."""

    #: Fewer than 3 erasures, and the unspent syndromes are zero: the rows
    #: taken as good, and the rows solved from them, are one codeword.
    CONSISTENT = "consistent"
    #: Exactly 3 erasures: every parity equation went into the solve, so
    #: there is nothing left to check the result against.
    UNVERIFIED = "unverified"
    #: Fewer than 3 erasures, and an unspent syndrome is not zero: some row
    #: taken as good (a CRC-good sector) is wrong.
    INCONSISTENT = "inconsistent"
    #: More than 3 erasures: nothing to solve, nothing to check.
    UNCORRECTABLE = "uncorrectable"
    #: No sector of the segment was read.
    MISSING = "missing"


@functools.cache
def _mul_table(k: int) -> bytes:
    """``x -> x * k`` over GF(256) for every byte ``x``, as a ``bytes.translate`` table."""
    return bytes(gmul(x, k) for x in range(256))


def _scaled(row: bytes, k: int) -> int:
    """``row`` with every byte multiplied by ``k``, as a big-endian int ready to XOR."""
    return int.from_bytes(row.translate(_mul_table(k)), "big")


def row_syndromes(rows: list[bytes]) -> list[int]:
    """The 3 syndromes of every column at once: :func:`syndromes`, a row at a time.

    ``rows[i]`` is codeword position ``i`` (equal widths, erased rows zeroed).
    Each result is one big-endian int whose byte ``c`` is column ``c``'s
    syndrome, so a column is a codeword exactly when its byte is zero in
    all three.
    """
    out: list[int] = []
    for rlog in ROOT_LOG:
        acc = 0
        for i, row in enumerate(rows):
            acc ^= _scaled(row, EXP[(rlog * i) % 255])
        out.append(acc)
    return out


def verify(seg: Segment) -> Parity:
    """Check ``seg`` against the parity its erasure solve did not use; never mutates it.

    The rows are the participating slots (BSM exclusions repacked out, as
    :func:`correct` does), an erased one zeroed. With no erasures the three
    syndromes must all be zero. With e = 1 or 2 the erased rows are solved
    from the first e syndromes as whole rows (the inverse of the e x e
    erasure matrix applied to the syndrome rows, which is the column solve
    of :func:`correct_codeword` done for all 1024 columns at once), and the
    other 3 - e syndromes must equal what those solved rows contribute.
    """
    participating = _participating(seg)
    if all(seg.sectors[i] is None for i in range(Segment.SECTORS)):
        log.debug("verify segment %d: no sectors; MISSING", seg.seg)
        return Parity.MISSING
    erased = erasure_positions(seg)
    m = len(erased)
    if m > REDUNDANCY:
        log.debug("verify segment %d: %d erasures; UNCORRECTABLE", seg.seg, m)
        return Parity.UNCORRECTABLE
    if m == REDUNDANCY:
        log.debug("verify segment %d: 3 erasures, no spare parity; UNVERIFIED", seg.seg)
        return Parity.UNVERIFIED
    width = _column_width(seg, participating)
    gone = set(erased)
    rows: list[bytes] = []
    for pos, slot in enumerate(participating):
        sec = seg.sectors[slot]
        if pos in gone or sec is None:
            rows.append(bytes(width))
        else:
            # A row of the wrong length is zero-filled to width, as
            # _gather_columns reads it.
            rows.append(sec.data.ljust(width, b"\x00")[:width])
    synd = row_syndromes(rows)
    if m:
        # S_j = sum_k M[j][k] X_k over the first m equations; X = M^-1 S.
        matrix = [[EXP[(ROOT_LOG[j] * erased[k]) % 255] for k in range(m)] for j in range(m)]
        try:
            inverse_cols = [
                _solve_linear(matrix, [int(j == u) for j in range(m)]) for u in range(m)
            ]
        except ValueError:
            log.debug("verify segment %d: erasure matrix singular; UNVERIFIED", seg.seg)
            return Parity.UNVERIFIED
        solved = []
        for k in range(m):
            acc = 0
            for u in range(m):
                acc ^= _scaled(synd[u].to_bytes(width, "big"), inverse_cols[u][k])
            solved.append(acc.to_bytes(width, "big"))
        # What the solved rows add to each unspent syndrome; the received
        # word's syndrome must be exactly that for the whole to be a codeword.
        spare = []
        for j in range(m, REDUNDANCY):
            expected = 0
            for k in range(m):
                expected ^= _scaled(solved[k], EXP[(ROOT_LOG[j] * erased[k]) % 255])
            spare.append(synd[j] ^ expected)
    else:
        spare = synd
    if any(spare):
        bad_columns = sum(1 for c in range(width) if any((s >> (8 * c)) & 0xFF for s in spare))
        log.debug(
            "verify segment %d: %d erasures, spare parity disagrees in %d of %d columns; "
            "INCONSISTENT",
            seg.seg,
            m,
            bad_columns,
            width,
        )
        return Parity.INCONSISTENT
    log.debug("verify segment %d: %d erasures, spare parity agrees; CONSISTENT", seg.seg, m)
    return Parity.CONSISTENT
