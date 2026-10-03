# tapewyrm-archive

The on-disk formats of the Tapewyrm recovery workflow, and nothing else:

| Module | Format | Written by | Read by |
|---|---|---|---|
| `tapewyrm_archive.twrf` | **TWRF** raw flux capture (header + GW flux stream + markers) | `tw dump`, `tw drive flux` | `tw convert` |
| `tapewyrm_archive.twti` | **TWTI** logical tape image (segments, states, provenance), written sparse; **TWTZ** = the same stream zstd-compressed (`.twtz`, like `.tar.zst`) | `tw convert` | qiclib, qicsilver |
| `tapewyrm_archive.twvl` | **TWVL** extracted backup volume (bytes + hole map) | extract | qicsilver |
| `tapewyrm_archive.types` | the types those files are made of (`CaptureHeader`, `Marker`, ...) | | |
| `tapewyrm_archive.progress` | the no-op progress hook protocol shared by every library | | |

It is the base of the package graph (`tapewyrm-cli` → archive ← `qiclib` ←
`qicsilver`), so it is **stdlib-only** (plus `backports.zstd` below Python 3.14:
the official backport of the stdlib `compression.zstd`, for TWTZ): no rich/click,
no hardware code. See `STYLE.md` at the repository root.

```bash
cd packages/tapewyrm-archive
uv sync --extra dev
uv run pytest
```
