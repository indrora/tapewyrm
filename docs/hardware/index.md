# Hardware

This section describes the hardware Tapewyrm talks to: the Greaseweazle v4.1
board, the floppy-interface tape drives, and the cartridges they read. Use it
to pick a drive profile, to check what a drive has done on the bench, and to
identify a cartridge before you dump it.

| Page | What it covers |
|---|---|
| [Greaseweazle v4.1](greaseweazle.md) | The board, the 34-pin lines Tapewyrm uses for QIC-117, the USB link limits, and what the Tapewyrm firmware adds to stock Greaseweazle firmware. |
| [Drives](drives/index.md) | Every drive profile, which drives have been tested, and the formats and rates each one reads. |
| [Cartridges](cartridges.md) | Cartridge types, lengths and recording formats, how the drive reports them, and cleaning cartridges. |

## The hardware chain

A tape is read through four pieces of hardware, each with its own failure modes:

```text
host (tw) --USB--> Greaseweazle v4.1 --34-pin cable--> tape drive --> cartridge
                   (Tapewyrm firmware)                 (12 V + 5 V from a PSU)
```

- The **host** runs `tw`, which owns every decision: which commands to send,
  when to capture, and what the captured flux means.
- The **Greaseweazle** sends QIC-117 command pulses, clocks back report bits,
  and streams raw flux. It does not interpret anything it reads.
- The **drive** speaks QIC-117 on the floppy control lines and puts MFM data
  on the read-data line while the tape moves.
- The **cartridge** holds the recording. Its length, coercivity and width
  decide which drives can read it.

!!! warning
    Floppy-tape drives need 12 V for the capstan motor. Power the drive from a
    separate supply, not from the Greaseweazle's 5 V header. See
    [Wire up a drive](../guide/wiring.md).

## See also

- [Wire up a drive](../guide/wiring.md)
- [Drive profiles](../tw/profiles.md)
- [QIC-117 command interface](../formats/qic-117.md)
