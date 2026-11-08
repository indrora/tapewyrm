# qiclib

Where data sits on a QIC tape and what it means: geometry, sector placement, merging, Reed-Solomon ECC, the header and volume table, cartridge and volume profiles, QIC-113 and QIC-122. No hardware; depends only on `tapewyrm-archive`.

Run its lint, type checks and tests from the repository root:

```sh
just check qiclib qiclib
```

Documentation:

- [Developer guide](../../docs/dev/qiclib.md)
- [Writing a volume profile](../../docs/dev/volume-profiles.md)
- [Formats](../../docs/formats/index.md)

Read [STYLE.md](../../STYLE.md) before changing code.
