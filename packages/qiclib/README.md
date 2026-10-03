# qiclib

Everything about **where data sits on a QIC tape and what it means**, with no
hardware attached. The physical layer (flux, PLL, MFM) lives in
[tapewyrm-cli](../tapewyrm-cli/README.md); it hands this library the sectors it
decoded and gets a TWTI tape image back. [qicsilver](../qicsilver/README.md)
uses it to read that image.

It depends only on [tapewyrm-archive](../tapewyrm-archive/README.md) and is
otherwise **stdlib-only**: no rich or click ([STYLE.md](../../STYLE.md) §2).

## Modules

| Module | What |
|---|---|
| `qiclib.types` | `RawSector`, `Segment`, `SegmentResult`, `FileEntry`, ... |
| `qiclib.geometry` | Serpentine track geometry and the sector-ID coordinate algebra. |
| `qiclib.place`, `qiclib.merge` | Sectors placed into segments by their own address; several passes merged. |
| `qiclib.rs`, `qiclib.segment` | QIC-80 Reed-Solomon erasure decoding, per-segment correction. |
| `qiclib.volume` | Header segment, bad-sector map and volume table (QIC-80-MC Rev N). |
| `qiclib.cartridge` + `profiles/cartridge/*.toml` | Which cartridge a header's geometry implies (QIC-40/80/3010/3020 lengths and widths, e.g. `qic-extra` = Verbatim MC3020EX QIC-Extra). |
| `qiclib.volume_profile` + `profiles/volume/*.toml` | Which backup software wrote the volume table, and where its fields are. |
| `qiclib.qic113`, `qiclib.qic113ext` | QIC-113 directories: Basic-DOS and extended (Rev G §8). |
| `qiclib.qic122` | QIC-122 (Stac LZS) decompression and QIC-113 compression extents. |
| `qiclib.build` | Sectors -> TWTI image: the layout half of `tw convert`. |
| `qiclib.identify` | What is on a tape, from a TWTI/TWTZ image (`qicsilver identify`). |
| `qiclib.extract` | TWTI/TWTZ image -> TWVL volumes (`qicsilver extract`). |
| `qiclib.testing` | Synthetic-tape builders and bench bytes, shared with the other packages' tests. |

## Profiles

Profiles are data, not code: a new cartridge or a new backup program is a new
TOML file.

- **Cartridge profiles** (`qiclib/profiles/cartridge/`) name the cartridge from
  the header's tracks x segments, e.g. `qic80-425ft`, `qic80-dc2120`,
  `qic3020-wide-750ft`, `qic-extra`.
- **Volume profiles** (`qiclib/profiles/volume/`) say how a volume table entry
  is laid out:

  | Profile | Volume tables written by |
  |---|---|
  | `qic80-rev-n` | Software that follows QIC-80-MC Rev N §8 exactly (vendor bit clear). |
  | `cms-qic113` | Colorado Memory Systems backup: vendor bit set, QIC-113 signature at byte 58. |
  | `mtn` | The program that writes "MTN" at byte 58 (vendor bit clear, label at byte 102, 4-byte extents). |
  | `vendor-unknown` | A vendor-specific entry with no known layout: bytes 0-56 only. |

  `qicsilver identify` and `qicsilver extract` score every profile against the
  volume table and pick the best (`--volume-profile guess`, the default);
  `--volume-profile NAME` or a path forces one, and `qicsilver identify --raw`
  shows the scoring.

## Develop

```sh
cd packages/qiclib
uv sync --extra dev
uv run pytest
```

Or, from the repository root, `just check qiclib qiclib`. See the
[root README](../../README.md) and the [format specifications](../../docs/spec/README.md).
