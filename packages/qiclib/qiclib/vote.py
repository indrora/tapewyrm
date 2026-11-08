"""Best out of N: rebuilding sectors no single read got right (``tw convert``).

The union (:mod:`qiclib.merge`) can only pick a read: a sector that no pass
read CRC-good stays bad, and a segment with more than 3 of those does not
correct. But a bad read is rarely bad everywhere. A dropout or a burst of
noise ruins a few dozen bytes of a 1024-byte sector and the CRC fails for
the whole of it, and the next read of the same tape ruins *different*
bytes. With several reads of one sector there are two ways to put the good
parts back together, and both are checked before anything is believed:

1. **Sector vote** (:func:`vote_sector`). With at least :data:`VOTE_QUORUM`
   copies of a sector, none CRC-good, take the most common byte at every
   position (data and stored CRC alike). If the result's CRC checks, it is
   the sector: CRC-CCITT over the data address mark and the data, exactly
   what the drive wrote (DESIGN.md §7.3; ``binascii.crc_hqx`` with preset
   0xFFFF is that CRC, the same as ``tapewyrm.codec.mfm.crc_ccitt``).
2. **RS-column rescue** (:func:`rescue_segment`). A segment that still has
   more than 3 erasures is solved one Reed-Solomon column at a time (byte
   ``c`` of each of its sectors is one codeword, QIC-80-MC Rev N 6.2.5). In
   a bad sector read at least twice, a byte every read agrees on is taken
   as known, and the rest are that column's erasures; a sector read once,
   or not at all, is an erasure in every column. A column with at most 2
   erasures is solved and must then satisfy the parity rows the solve did
   not use (:mod:`qiclib.rs`, "spare parity"), so nothing is taken on trust:
   a byte several reads agree on but got wrong anyway shows as a column that
   does not check, and the rescue fails rather than invent data. The
   segment is rescued only if every column checks.

Neither step ever replaces a CRC-good sector, and the rescue never touches
a segment that corrects without it. Only ID-CRC-good copies take part: a
sector whose ID failed its CRC may belong somewhere else.

TODO: a column with exactly 3 erasures solves but cannot be checked, so
the rescue gives up on it. A majority vote over the disagreeing bytes of
reads >= 3 (to bring the column under 3) would get more of them; it was
left out because "every read agrees" is the rule the rescue was asked for.
"""

from __future__ import annotations

import binascii
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from dataclasses import replace as copy_of

from qiclib import rs
from qiclib.types import RawSector, Segment

log = logging.getLogger(__name__)

#: Copies of a CRC-bad sector needed before a byte-wise vote: 2 of 3 is the
#: smallest true majority. With 2 copies that differ there is no majority to
#: take (the RS-column rescue still uses where they agree).
VOTE_QUORUM = 3

# The data address mark the data CRC covers: 3 x A1 (missing-clock sync)
# then FB, or F8 for a deleted-data (format-time bad) block. DESIGN.md §7.3;
# the same bytes as tapewyrm.codec.mfm A1 / DAM_NORMAL / DAM_DELETED.
_SYNC = b"\xa1\xa1\xa1"
_DAM_NORMAL = 0xFB
_DAM_DELETED = 0xF8

Key = tuple[int, int, int]  # (fsd, ftk, fsc): a sector's own address


def key_of(sec: RawSector) -> Key:
    return (sec.fsd, sec.ftk, sec.fsc)


def data_crc(data: bytes, deleted: bool) -> int:
    """The CRC the drive would have written after ``data`` (DESIGN.md §7.3)."""
    mark = bytes([_DAM_DELETED if deleted else _DAM_NORMAL])
    return binascii.crc_hqx(_SYNC + mark + data, 0xFFFF)


@dataclass
class VoteStats:
    """What voting and rescue did, for ``tw convert``'s per-track log.

    ``suspect`` counts coordinates with no CRC-good read; ``tried`` those of
    them with enough copies to vote; ``voted`` the coordinates whose vote
    CRC-checked. ``rescued`` maps a segment the column rescue saved to the
    sectors it rebuilt, and ``rebuilt`` holds their coordinates;
    ``unrescued`` maps one it tried and could not save to (columns that
    checked, columns in all).
    """

    suspect: int = 0
    tried: int = 0
    voted: set[Key] = field(default_factory=set)
    rescued: dict[int, int] = field(default_factory=dict)
    rebuilt: set[Key] = field(default_factory=set)
    unrescued: dict[int, tuple[int, int]] = field(default_factory=dict)


def suspect_copies(passes: Iterable[Iterable[RawSector]]) -> dict[Key, list[RawSector]]:
    """Every ID-good copy of each coordinate no pass read CRC-good, in pass order.

    The raw material for both steps: references to the passes' own sectors,
    nothing copied. A coordinate with a CRC-good copy anywhere is left out
    (the union already has it right), as is a copy whose ID CRC failed.
    """
    good: set[Key] = set()
    copies: dict[Key, list[RawSector]] = {}
    for sectors in passes:
        for sec in sectors:
            key = key_of(sec)
            if sec.data_crc_ok:
                good.add(key)
            elif sec.id_crc_ok:
                copies.setdefault(key, []).append(sec)
    suspects = {k: v for k, v in copies.items() if k not in good}
    log.debug(
        "%d coordinates read only CRC-bad (%d copies among them)",
        len(suspects),
        sum(len(v) for v in suspects.values()),
    )
    return suspects


def _majority(datas: list[bytes]) -> bytes:
    """The most common byte at every position of equal-length ``datas``.

    Most positions agree in every copy, so the disagreeing ones are found
    first with one big-int XOR per copy, and only those are counted.
    """
    base = datas[0]
    first = int.from_bytes(base, "big")
    diff = 0
    for other in datas[1:]:
        diff |= first ^ int.from_bytes(other, "big")
    if not diff:
        return base
    out = bytearray(base)
    for i, b in enumerate(diff.to_bytes(len(base), "big")):
        if b:
            out[i] = Counter(d[i] for d in datas).most_common(1)[0][0]
    return bytes(out)


def vote_sector(copies: list[RawSector], quorum: int = VOTE_QUORUM) -> RawSector | None:
    """A byte-wise majority of ``copies`` (all CRC-bad), or None unless its CRC checks.

    Data, stored CRC and the deleted-data flag are each voted. The result is
    accepted if its CRC equals the voted CRC or the CRC any copy stored (a
    copy can have the data right where the vote over the CRC bytes went the
    other way; a false match is 1 in 65,536 per distinct stored value).
    Copies without a stored CRC (not from flux) or of the wrong length do
    not count toward ``quorum``.
    """
    usable = [c for c in copies if c.data_crc is not None and len(c.data) == RawSector.SIZE]
    if len(usable) < quorum:
        return None
    data = _majority([c.data for c in usable])
    deleted = sum(c.deleted for c in usable) * 2 > len(usable)
    stored = Counter(c.data_crc for c in usable)
    computed = data_crc(data, deleted)
    if computed != stored.most_common(1)[0][0] and computed not in stored:
        log.debug(
            "vote over %d copies of %s: CRC %04x matches no stored CRC; no vote",
            len(usable),
            key_of(usable[0]),
            computed,
        )
        return None
    log.debug(
        "vote over %d copies of %s: CRC %04x checks", len(usable), key_of(usable[0]), computed
    )
    return copy_of(usable[0], data=data, data_crc=computed, data_crc_ok=True, deleted=deleted)


def vote_sectors(
    suspects: dict[Key, list[RawSector]],
    *,
    quorum: int = VOTE_QUORUM,
    stats: VoteStats | None = None,
) -> list[RawSector]:
    """:func:`vote_sector` over every suspect coordinate: the sectors it rebuilt.

    Hand them to the union as one more pass, last: each is CRC-good where
    no read was, so it fills exactly those coordinates and nothing else.
    """
    tally = stats if stats is not None else VoteStats()
    tally.suspect = len(suspects)
    out: list[RawSector] = []
    for key, copies in suspects.items():
        if len(copies) < quorum:
            continue
        tally.tried += 1
        voted = vote_sector(copies, quorum)
        if voted is not None:
            tally.voted.add(key)
            out.append(voted)
    log.debug(
        "vote: %d coordinates read only CRC-bad, %d with %d+ copies, %d rebuilt",
        tally.suspect,
        tally.tried,
        quorum,
        len(out),
    )
    return out


# ---------------------------------------------------------------------------
# RS-column rescue
# ---------------------------------------------------------------------------


def rescue_segment(
    seg: Segment, suspects: dict[Key, list[RawSector]], *, stats: VoteStats | None = None
) -> int | None:
    """Rebuild ``seg``'s bad sectors column by column; the count rebuilt, or None.

    For a segment that does not correct as it is (more than 3 erasures).
    Each participating slot is one of: a trusted row (CRC-good, not
    deleted); a partly known row (CRC-bad, read at least twice: bytes every
    read agrees on are known, the rest are erasures in their column); or an
    erased row (missing, read once, or a deleted-data block). Every column
    must then have at most 2 erasures, solve, and satisfy the spare parity;
    if any column fails, ``seg`` is left exactly as it was and None comes
    back. On success every row that was not trusted is replaced, in
    ``seg.sectors``, by a CRC-good copy holding the solved bytes (a deleted
    block stays as it is, for the ordinary correction to solve as before),
    and the segment corrects without erasures from them.
    """
    tally = stats if stats is not None else VoteStats()
    participating = [i for i in range(Segment.SECTORS) if i not in seg.excluded]
    n = len(participating)
    present = next((s for s in seg.sectors if s is not None), None)
    if present is None:
        log.debug("rescue segment %d: nothing read; nothing to rescue", seg.seg)
        return None
    # A segment is 32 consecutive FSCs of one floppy track (DESIGN.md §2.3),
    # so any sector read in it gives the address of every slot.
    present_slot = seg.sectors.index(present)
    fsd, ftk, fsc0 = present.fsd, present.ftk, present.fsc - present_slot
    width = RawSector.SIZE

    known: list[bytes] = []  # per position: the row, zero where not known
    agree: list[int | None] = []  # per position: big-int, byte 0xFF where known; None = all
    always_erased: list[int] = []
    rebuild: list[int] = []  # positions whose row the rescue will replace
    for pos, slot in enumerate(participating):
        sec = seg.sectors[slot]
        if sec is not None and sec.data_crc_ok and not sec.deleted and len(sec.data) == width:
            known.append(sec.data)
            agree.append(None)
            continue
        if sec is not None and sec.deleted:
            known.append(bytes(width))
            agree.append(0)
            always_erased.append(pos)
            continue
        rebuild.append(pos)
        copies = [
            c
            for c in suspects.get((fsd, ftk, fsc0 + slot), [])
            if len(c.data) == width and not c.deleted
        ]
        if len(copies) < 2:
            known.append(bytes(width))
            agree.append(0)
            always_erased.append(pos)
            continue
        first = int.from_bytes(copies[0].data, "big")
        diff = 0
        for other in copies[1:]:
            diff |= first ^ int.from_bytes(other.data, "big")
        mask = bytes(0 if b else 0xFF for b in diff.to_bytes(width, "big"))
        known.append(bytes(a & m for a, m in zip(copies[0].data, mask, strict=True)))
        agree.append(int.from_bytes(mask, "big"))
    if len(always_erased) > rs.REDUNDANCY - 1:
        log.debug(
            "rescue segment %d: %d rows erased in every column (missing, read once or "
            "deleted); a checked column needs at most 2; giving up",
            seg.seg,
            len(always_erased),
        )
        tally.unrescued[seg.seg] = (0, width)
        return None

    # Per position, which columns it is erased in (bytes of a mask, 1 = erased).
    erased_in = [
        bytes(width * [1])
        if a == 0
        else bytes(width)
        if a is None
        else bytes(0 if b else 1 for b in a.to_bytes(width, "big"))
        for a in agree
    ]
    solved_rows = [bytearray(row) for row in known]
    ok = 0
    for c in range(width):
        erased = [pos for pos in range(n) if erased_in[pos][c]]
        if len(erased) > rs.REDUNDANCY - 1:
            continue  # unsolvable with a check left over; counted below
        column = [known[pos][c] for pos in range(n)]
        if erased:
            column = rs.correct_codeword(column, erased, n)
        if any(rs.syndromes(column, n)):
            continue
        ok += 1
        for pos in erased:
            solved_rows[pos][c] = column[pos]
    if ok < width:
        log.debug(
            "rescue segment %d: %d of %d columns solved and checked; not rescued",
            seg.seg,
            ok,
            width,
        )
        tally.unrescued[seg.seg] = (ok, width)
        return None

    # `rebuild` never holds a deleted block (it stays an erasure for the
    # ordinary correction to solve, as without the rescue).
    for pos in rebuild:
        slot = participating[pos]
        data = bytes(solved_rows[pos])
        old = seg.sectors[slot]
        tally.rebuilt.add((fsd, ftk, fsc0 + slot))
        seg.sectors[slot] = (
            copy_of(old, data=data, data_crc_ok=True, data_crc=data_crc(data, False))
            if old is not None
            else RawSector(
                fsd=fsd, ftk=ftk, fsc=fsc0 + slot, data=data, id_crc_ok=True,
                data_crc_ok=True, deleted=False, data_crc=data_crc(data, False),
            )
        )  # fmt: skip
    tally.rescued[seg.seg] = len(rebuild)
    log.debug(
        "rescue segment %d: all %d columns checked; %d sectors rebuilt",
        seg.seg,
        width,
        len(rebuild),
    )
    return len(rebuild)
