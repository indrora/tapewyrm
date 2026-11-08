# tapewyrm-archive

Tapewyrm's on-disk formats (TWRF, TWTI/TWTZ, TWVL), the QIC-117 report decoders and the progress-hook protocol shared by every other package. Stdlib-only, plus `backports.zstd` below Python 3.14.

Run its lint, type checks and tests from the repository root:

```sh
just check tapewyrm-archive tapewyrm_archive
```

Documentation:

- [Developer guide](../../docs/dev/tapewyrm-archive.md)
- [TWS specifications](../../docs/spec/README.md)

Read [STYLE.md](../../STYLE.md) before changing code.
