# Host software

The host side of Tapewyrm is five Python packages under `packages/`. Each is a
separate uv project with its own tests, and they depend on each other through
editable path sources. This page maps them; each package has its own page.

| Package | Provides | Depends on | Page |
|---|---|---|---|
| `tapewyrm-archive` | The on-disk formats (TWRF, TWTI/TWTZ, TWVL), QIC-117 report decoders, the progress protocol. Stdlib only. | None | [tapewyrm-archive](tapewyrm-archive.md) |
| `qiclib` | The QIC formats with no hardware: geometry, merge, Reed-Solomon, header, volume table, QIC-113, QIC-122, cartridge and volume profiles. Stdlib only. | `tapewyrm-archive` | [qiclib](qiclib.md) |
| `tapewyrm-cli` | `tw`: the device link, the QIC-117 drive layer, drive profiles, `dump`, the flux/MFM codec, `convert`, flashing. | `tapewyrm-archive`, `qiclib` | [tapewyrm-cli](tapewyrm-cli.md) |
| `qicsilver` | `qicsilver`: `identify`, `extract`, `inspect`, `tar` on tape images. | `tapewyrm-archive`, `qiclib` | [qicsilver](qicsilver.md) |
| `cinnabar` | The Qt viewer for captures and images. | `tapewyrm-archive`, `qiclib`, `tapewyrm-cli` | [cinnabar](cinnabar.md) |

The dependency graph and the rules that keep the base packages stdlib-only are
in the [Codebase overview](index.md#package-dependencies).

## Profiles

Two kinds of TOML profile change behaviour without code:

- **Drive profiles** (`tapewyrm-cli`): wake-up sequences and QIC-117 timings
  for a drive. See [tw drive profiles](../tw/profiles.md) and
  [tapewyrm-cli](tapewyrm-cli.md#drive-profiles).
- **Cartridge and volume profiles** (`qiclib`): tape geometry, and how a
  backup program laid out its volume table and file sets. See
  [Writing a volume profile](volume-profiles.md).

## Test a package

```sh
just check qiclib qiclib
```

`just host` checks every package in dependency order. See
[Building and testing](building.md).
