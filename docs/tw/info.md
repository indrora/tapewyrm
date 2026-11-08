# tw info

Shows which `tw` and firmware builds are in use, and which board is attached.

## Synopsis

```text
tw [GLOBAL OPTIONS] info
```

## Description

`tw info` talks to the Greaseweazle only, never the drive. Run it first, to
check that the board is attached and runs the Tapewyrm firmware.

Unlike every other command that opens the board, `tw info` accepts stock
Greaseweazle firmware: it describes it instead of refusing it. That makes it
the way to check whether a flash took.

It uses the global `--port` option. `--profile` has no effect.

## Options

| Option | Default | Description |
|---|---|---|
| `--help` | | Show help and exit. |

## Output

```text
tw        : 0.1.0, commit 5ed5c63a1b2c [git checkout]
device    : tapewyrm-GW V4.1
port      : /dev/cu.usbmodem1101
mcu       : 288 MHz, 224 kB SRAM
firmware  : 1.6, commit 5ed5c63a1b2c, protocol v1 [capture, markers, verbs]
```

(Example output.)

| Field | Description |
|---|---|
| `tw` | The host tool's version, the commit it was built from, and where that came from. `(+uncommitted changes)` follows the commit when the tree was dirty. |
| `device` | The board model the firmware reports. |
| `port` | The serial port used, and the USB serial number when there is one. |
| `mcu` | The microcontroller's clock and SRAM, as the firmware reports them. |
| `firmware` | The firmware version, the commit it was built from, the Tapewyrm protocol version and the capabilities it offers. On stock firmware this reads `VERSION, stock Greaseweazle (no Tapewyrm verbs)`. `commit unknown (pre-BUILD_INFO image)` means the image predates the build-info command. |
| `note` | Printed only when `tw` and the firmware were built from different commits. That is allowed; rebuild one of them if a command misbehaves. |

## Examples

Check the attached board:

```sh
tw info
```

Check a specific board when several are attached:

```sh
tw --port /dev/cu.usbmodem1101 info
```

## Exit status

`0` on success. `1` if no board is found, several are found without `--port`,
or the board does not answer.

## See also

- [Flash the Greaseweazle](../guide/flash-greaseweazle.md)
- [tw update and tw dfu](update.md)
- [Greaseweazle v4.1](../hardware/greaseweazle.md)
