# tapewyrm-cli (`tw`)

The hardware half of Tapewyrm. `tw` talks to a Greaseweazle v4.1 running Tapewyrm firmware, drives a QIC-117 tape drive, dumps tracks to TWRF captures, converts them to TWTI/TWTZ images, and flashes the firmware.

Run its lint, type checks and tests from the repository root:

```sh
just check tapewyrm-cli tapewyrm
```

Documentation:

- [Developer guide](../../docs/dev/tapewyrm-cli.md)
- [tw command reference](../../docs/tw/index.md)
- [Drive profiles](../../docs/tw/profiles.md)

Read [STYLE.md](../../STYLE.md) before changing code.
