# Tapewyrm firmware

QIC-117 / QIC-80 floppy-tape firmware for the Greaseweazle v4.1 (AT32F403). It is the Greaseweazle v1.6 firmware, vendored complete, with the QIC verbs and free-running flux capture grafted onto its command loop.

Build it with PlatformIO from the repository root:

```sh
just fw
```

Documentation:

- [Firmware developer guide](../docs/dev/firmware.md): the graft, layout, building, the vendor seam, `TODO(bench)` items
- [Device protocol](../docs/dev/protocol.md): `protocol/protocol.toml` and the generated `inc/protocol.h`
- [Flash the Greaseweazle](../docs/guide/flash-greaseweazle.md)
- [vendor-seam/README.md](vendor-seam/README.md): the Greaseweazle primitives kept pristine

Read [STYLE.md](../STYLE.md) before changing code.
