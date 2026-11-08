# QIC standards

This section summarises the QIC standards as far as Tapewyrm uses them, and
the vendor structures that real backup software wrote on top of them. Each
page gives the layout, cites the standard and section it comes from, and
notes where Tapewyrm departs from the standard or relies on bench
observation.

The standards themselves are in `docs/qic-standards/`; the
[QIC standards library](standards.md) lists every PDF.

## The layer stack

A floppy tape is read through a stack of formats. Each layer consumes the
one below it, and each is handled by one part of the code.

| Layer | Standard | What it defines | Implemented in |
|---|---|---|---|
| Command interface | [QIC-117](qic-117.md) | Pulse-count commands on Step, report bits on Track Zero, cue pulses on Index. | Firmware `src/qic/qic.c`; `tapewyrm.qic117` (commands, drive, profiles); `tapewyrm_archive.qic117` (report decoders) |
| Recording format | [QIC-80-MC](qic-80-mc.md), [QIC-3010/3020](qic-3010-3020.md) | Serpentine tracks, 32-sector segments, MFM, sector IDs, the coordinate algebra. | `tapewyrm.codec.gwstream`, `gwpll`, `mfm` (flux to sectors); `qiclib.merge`, `qiclib.place`, `qiclib.geometry` |
| Error correction | [ECC](ecc.md) | Reed-Solomon over GF(256), 3 parity sectors per segment, erasure decoding. | `qiclib.segment`, `qiclib.rs` |
| Header segment | [Header and bad-sector map](header.md) | Format parameter record, geometry, the bad-sector map. | `qiclib.header` |
| Volume table | [Volume table](volume-table.md) | 128-byte `VTBL` entries: one per backup volume. | `qiclib.volume.table`, `qiclib.volume.profile` |
| Compression | [QIC-122](qic-122.md) | Stac LZS frames inside QIC-113 compression extents. | `qiclib.volume.qic122`, `qiclib.extract` |
| File sets | [QIC-113](qic-113.md) | Directory and data sections: the files themselves. | `qiclib.volume.qic113`, `qiclib.volume.qic113ext`, `qiclib.volume.fileset` |
| Vendor layouts | [Vendor formats](vendor/index.md) | What specific backup programs wrote in the parts the standards leave open. | Volume profiles in `qiclib/volume/profiles/` |

QIC-122 sits below QIC-113 in the read order: a compressed volume is
decompressed first, then its directory and data sections are parsed. The
standards describe it the other way round because QIC-113 §9 defines the
extents that carry the QIC-122 stream.

## Where the tools cut the stack

```text
drive --QIC-117--> tw dump ----> TWRF   (flux, one file per track pass)
                   tw convert -> TWTI   (placed, BSM-applied, RS-corrected segments)
                   qicsilver extract -> TWVL (one volume, decompressed, with holes)
                   qicsilver tar ----> tar  (files)
```

- `tw dump` captures flux and stores it unchanged. See
  [TWS-1 TWRF](../spec/twrf.md).
- `tw convert` does the recording, ECC and header layers once, and writes a
  logical tape image. See [TWS-2 TWTI/TWTZ](../spec/twti.md).
- `qicsilver extract` reads the volume table through a volume profile and
  decompresses the volume. See [TWS-3 TWVL](../spec/twvl.md).
- `qicsilver tar` parses the file set and writes a tar with a damage report.

Everything above the TWTI is offline and needs no hardware, so a tape is
decoded once and can be re-extracted as profiles improve.

## Conventions on these pages

- Offsets are decimal, with hex in parentheses where the standard uses hex.
- Multi-byte fields are little-endian unless a page says otherwise.
- "Word" is 2 bytes, "doubleword" 4, "quadword" 8, as in the standards.
- "Bench" marks something observed on real tapes or drives and not stated by
  a standard.

## See also

- [QIC standards library](standards.md)
- [qiclib](../dev/qiclib.md)
- [Writing a volume profile](../dev/volume-profiles.md)
