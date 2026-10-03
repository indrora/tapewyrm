# qiclib

Everything about **where data sits on a QIC tape and what it means**, with no
hardware attached. The physical layer (flux, PLL, MFM) lives in
`tapewyrm-cli`; it hands this library `RawSector`s and gets a TWTI tape image
back.

| Module | What |
|---|---|
| `qiclib.types` | `RawSector`, `Segment`, `SegmentResult`, `FileEntry`, ... |
| `qiclib.geometry`, `place`, `merge` | serpentine geometry; sectors placed by their own address; passes merged |
| `qiclib.rs`, `segment` | QIC-80 Reed-Solomon ECC, per-segment correction |
| `qiclib.volume` | header segment, bad-sector map, volume table (QIC-80 Rev N) |
| `qiclib.cartridge` | which cartridge a geometry implies |
| `qiclib.tape_profile` + `profiles/tape/*.toml` | per-software volume-table layouts (Rev N, CMS, MTN, ...) |
| `qiclib.qic113`, `qic113ext`, `qic122` | backup directory formats and QIC-122 decompression |
| `qiclib.build` | sectors -> TWTI image (the layout half of `tw convert`) |
| `qiclib.identify` | what is on a tape, from a TWTI image |
| `qiclib.extract` | TWTI image -> TWVL volumes |
| `qiclib.testing` | synthetic-tape builders and bench bytes, shared with other packages' tests |

Depends only on `tapewyrm-archive`. Stdlib-only otherwise: no rich/click (see
`STYLE.md` §2 at the repository root).

```bash
cd packages/qiclib
uv sync --extra dev
uv run pytest
```
