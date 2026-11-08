# Software overview

Tapewyrm has three command-line tools. Each one owns one stage of a recovery,
and each stage writes a file the next one reads, so you can stop after any
step and continue later on another machine.

| Tool | Stage | Reads | Writes | Needs hardware |
|---|---|---|---|---|
| [`tw`](tw/index.md) | Capture and convert | The drive, through the Greaseweazle; TWRF captures | TWRF captures; TWTI/TWTZ tape images | `dump`, `info`, `drive`, `update`, `dfu` only |
| [`qicsilver`](qicsilver/index.md) | Recover | TWTI/TWTZ tape images | TWVL volumes; tar files and damage reports | No |
| [`cinnabar`](cinnabar/index.md) | Inspect | TWRF captures; TWTI/TWTZ tape images | Nothing (a viewer); optional screenshots | No |

## Which tool do I need?

- **Reading a tape for the first time:** `tw` to dump and convert it, then
  `qicsilver` to get the files out. [Read a tape](guide/read-a-tape.md) walks
  through the whole sequence.
- **A tape image that won't identify or extract cleanly:** `qicsilver
  identify` and `qicsilver inspect` to see what it is, then
  [qicsilver read overrides](qicsilver/overrides.md) if no profile fits.
- **Damaged or marginal reads:** `cinnabar` to see where a track goes wrong,
  flux to bytes, and which of several passes read each segment best.

## Running the tools

All three run from the source tree with `uv`, with no install step:

```sh
uv run --project packages/tapewyrm-cli tw --help
uv run --project packages/qicsilver qicsilver --help
uv run --project packages/cinnabar cinnabar --help
```

With `just`, the same commands are `just tapewyrm ARGS`, `just qicsilver ARGS`
and `just cinnabar ARGS`. See [Install the tools](guide/install.md).

## See also

- [tw overview](tw/index.md)
- [qicsilver overview](qicsilver/index.md)
- [cinnabar overview](cinnabar/index.md)
- [TWS specifications](spec/README.md): the TWRF, TWTI/TWTZ and TWVL formats
  that pass between the tools
