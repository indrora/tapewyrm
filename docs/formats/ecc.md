# Error correction

Every QIC-40, QIC-80, QIC-3010 and QIC-3020 segment carries three sectors of
Reed-Solomon parity. Because each sector also has its own CRC, a decoder
knows which sectors are bad before it starts, and can rebuild up to three of
them per segment. This erasure decoding is what makes a damaged tape
recoverable, and why reading the same track more than once helps.

Source: QIC-80-MC Rev N §6.2 ([qic80n.pdf](../qic-standards/qic80n.pdf)).
QIC-3010-MC Rev H and QIC-3020-MC Rev H §6.2 define the same code.

## Layout

The 32 sectors of a segment form a 32 × 1,024 byte matrix: one row per
sector, one column per byte position. Each **column** is an independent
Reed-Solomon codeword (Rev N §6.2.1):

| Rows | Content |
|---|---|
| 0–28 | Data sectors |
| 29–31 | Parity sectors |

Data is written row by row, starting with row 0, and within a row from
column 0. So one bad sector costs one symbol in each of the 1,024 codewords,
at the same position in all of them.

## The code

| Item | Value | Source |
|---|---|---|
| Symbol | 8 bits, GF(256) | Rev N §6.2.2 |
| Field polynomial | `f(x) = x^8 + x^7 + x^2 + x + 1` (reduction modulus `0x187`) | Rev N §6.2.2 |
| Generator polynomial | `g(x) = x^3 + r^105·x^2 + r^105·x + 1`, with `r^105 = 0xC0` | Rev N §6.2.3 |
| Redundancy | 3 symbols per codeword | Rev N §6.2.1 |
| Codeword | `d(x) = Σ d_i·x^i` for i = 0..N, divisible by `g(x)`; N = 31 less the number of bad blocks in the segment | Rev N §6.2.3 |
| Test vectors | Seven columns of known data and parity | Rev N §6.2.4, Figure 6.3 |

With the primitive element `α = 0x02`, the three roots of `g(x)` are `0x01`,
`0x02` and `0xC3`; they are the points at which the syndromes are evaluated.

## What it corrects

Rev N §6.2 states the correction capability per segment:

| Corrects | Detects but may not correct |
|---|---|
| Up to 3 sectors with CRC errors | 2 sectors with CRC failures |
| 1 sector with a CRC error and 1 with a CRC failure | 2 sectors with CRC errors and 1 with a CRC failure |
| 1 sector with a CRC failure | |

A *CRC error* is a sector whose CRC check failed, so its position is known.
A *CRC failure* is an error the CRC did not catch, so its position is not
known. Known positions (erasures) cost one parity symbol each; unknown ones
cost two.

## Excluded sectors

Sectors the bad-sector map excludes are not part of the codeword (Rev N
§6.2.5). The data fills the non-excluded sectors in order, and the parity
occupies the **last three non-excluded** sectors. A segment with k excluded
sectors therefore has a codeword of 32 − k symbols and 29 − k data sectors.

Example from Rev N Figure 6.4, as it appears on tape (D data, B excluded,
P parity):

```text
D D D D D D D D D D D D D B D B D D D D D D D D D D D D D P P P
```

Excluded sectors are skipped when the segment is gathered into the matrix,
and the decoder works on the packed codeword.

!!! warning
    Apply the bad-sector map before correction. Treating an excluded sector as
    a damaged data sector puts every symbol of the codeword in the wrong
    position, and the correction "succeeds" with wrong data.

## Tapewyrm notes

**Modules.** `qiclib.rs` is the field arithmetic, the erasure solver and the
parity check. `qiclib.segment` builds the erasure mask from each sector's
CRC and returns the 29 data sectors in order.

**Erasure-only decoding.** A sector is an erasure if it is missing, its data
CRC failed, or it carries a deleted-data mark. With the erasure set E known
(|E| ≤ 3), `qiclib.rs` computes three syndromes per column and solves the
|E| × |E| linear system over GF(256) for the missing symbols. It does not
search for unknown-position errors.

| Erasures | Segment state |
|---|---|
| 0 | Clean: data returned as read, no solve |
| 1–3 | Corrected |
| More than 3 | Uncorrectable: the readable sectors are kept and the rest zero-filled |
| All 32 slots empty | Missing |

**Spare parity is checked.** With fewer than three erasures, parity is left
over. `qiclib.rs.verify` solves with the parity it needs and checks the rest.
A disagreement means a sector with a good CRC is still wrong, or the segment
was never a codeword at all.

**Unwritten segments.** A segment the format wrote but no backup ever
overwrote holds the format fill byte in every data sector, with no parity
computed. Every such segment fails the spare-parity check. `tw convert`
reports these as "unwritten (format fill)", not as damage. On the bench a
QIC-80 tape filled with `6B` (Colorado's fill byte, QIC-117 Rev J vendor
table) showed this from the middle of one track to the end of the tape.

**Multiple reads.** Each pass over a track gives another chance at each
sector. `qiclib.merge` keeps a CRC-good copy of every sector from any pass
before correction, so three bad sectors spread over several reads can still
correct.

**Test vectors.** The unit tests use Rev N Figure 6.3: one column has data rows
0–28 equal to `01` through `1D` (the last column of the figure) and parity rows 29–31 equal to `5D FF A3`.
Erasing any three or fewer positions must restore it exactly.

## See also

- [QIC-80-MC recording format](qic-80-mc.md)
- [Header and bad-sector map](header.md)
- [Passes and merged reads](../cinnabar/passes.md)
- [tw convert](../tw/convert.md)
