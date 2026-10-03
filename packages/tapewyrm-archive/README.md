# tapewyrm-archive

The on-disk formats of the Tapewyrm recovery pipeline, and the small pieces
every other package shares. It sits at the base of the package graph, so it is
**stdlib-only**: no rich or click, and no hardware code. The one exception is
`backports.zstd` on Python below 3.14, the official backport of the stdlib
`compression.zstd` (PEP 784) used for TWTZ. See [STYLE.md](../../STYLE.md) §2.

```
tapewyrm-cli ──► tapewyrm-archive ◄── qiclib ◄── qicsilver
```

## Modules

| Module | What | Written by | Read by |
|---|---|---|---|
| `tapewyrm_archive.twrf` | **TWRF** raw flux capture: header (rate, drive identity, commits) + Greaseweazle flux stream + Tapewyrm markers. Spec: [TWS-1](../../docs/spec/twrf.md). | `tw dump`, `tw drive flux` | `tw convert` |
| `tapewyrm_archive.twti` | **TWTI** logical tape image: segments, per-sector state, provenance, written sparse. **TWTZ** (`.twtz`) is the same stream zstd-compressed. Spec: [TWS-2](../../docs/spec/twti.md). | `tw convert` | qiclib, `qicsilver` |
| `tapewyrm_archive.twvl` | **TWVL** one extracted backup volume: bytes + hole map. Spec: [TWS-3](../../docs/spec/twvl.md). | `qicsilver extract` | `qicsilver tar` |
| `tapewyrm_archive.types` | The types those files are made of (`CaptureHeader`, `Marker`, ...). | | |
| `tapewyrm_archive.qic117` | Decoders for the QIC-117 report bytes stored in TWRF/TWTI headers (drive status, configuration, tape status, vendor ID). | | `tw`, qiclib |
| `tapewyrm_archive.progress` | The progress-hook protocol (`Progress`, `NULL_PROGRESS`) every library loop reports through. | | |
| `tapewyrm_archive._zstd` | Zstandard: stdlib `compression.zstd`, or its backport. Import zstd only through here. | | |

## Develop

```sh
cd packages/tapewyrm-archive
uv sync --extra dev
uv run pytest
```

Or, from the repository root, `just check tapewyrm-archive tapewyrm_archive`
(lint, format check, mypy and tests). See the [root README](../../README.md)
for the whole project.
