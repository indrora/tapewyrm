# Interface (Greaseweazle v4.1)

This page describes the Greaseweazle v4.1 as Tapewyrm uses it: the
microcontroller, the 34-pin lines that carry QIC-117, the limits of the USB
link, and what the Tapewyrm firmware adds to the stock Greaseweazle firmware.
Tapewyrm supports this one board and needs no hardware changes to it.

## Board

| Item | Value | Notes |
|---|---|---|
| Microcontroller | Artery AT32F403A (`at32f403acgu7`), Cortex-M4 | From the board definition, `firmware/boards/greaseweazle_v4_at32f403a.json`. |
| Clock | 216 MHz | `f_cpu` in the board definition. |
| SRAM | 224 KB | Most of it is the flux ring buffer for captures. |
| USB | USB-C, Full-Speed (12 Mbit/s) | See [USB link limits](#usb-link-limits). |
| Floppy outputs | Buffered, 40 mA | Strong enough for drive-side pull-ups. |
| Power | 5 V header; USB 5 V isolation jumper | The 5 V header cannot run a tape drive's motor. |
| Recovery | Hardware DFU header | Straps the AT32's ROM bootloader, so a broken flash is always recoverable. See [tw flash and tw dfu](../tw/flash.md). |

## 34-pin lines used for QIC-117

A floppy-tape drive multiplexes two channels over the floppy cable. The
command channel (QIC-117) reuses the floppy control lines; the data channel is
ordinary MFM on the read-data line. The Greaseweazle already drives and senses
every line involved in the same direction a floppy drive needs, so no
rewiring is required.

Pin numbers and directions are from QIC-117 Rev J Table I-b
([qic117j.pdf](../qic-standards/qic117j.pdf)). "In" and "out" are from the
drive's point of view, as the standard gives them.

| Pin | Signal | Drive I/O | QIC-117 use | Used by Tapewyrm |
|---|---|---|---|---|
| 8 | Index | Out | Cue pulses (ready, report bit presented, waiting for argument); once per segment during Logical Forward | Yes: idle cue and segment boundary |
| 10 | Drive Select 0 | In | Hardware select | Through Greaseweazle `SELECT` |
| 12 | Drive Select 1 | In | Hardware select | Through Greaseweazle `SELECT` |
| 14 | Drive Select 2 | In | Hardware select | Through Greaseweazle `SELECT` |
| 6 | Drive Select 3 | In | Hardware select | Not used |
| 16 | Motor On | In | Motor-on wake on some drives | `insight` profile only |
| 18 | Direction | In | Not used by QIC-117 | Not used |
| 20 | Step | In | Command and argument pulse trains | Yes: every command |
| 22 | Write Data | In | Data channel, writing | Never driven |
| 24 | Write Enable | In | Data channel, writing | Never asserted |
| 26 | Track Zero | Out | Serial report bits, LSB first | Yes: every report |
| 28 | Write Protect | Out | Cartridge write protect | Read by `tw drive scope` |
| 30 | Read Data | Out | MFM flux during motion | Yes: captures |
| 32 | Side Select | In | Not used by QIC-117 | Not used |
| 34 | Diskette Change | Out | Not used by QIC-117 | Not used |

Odd pins are ground. QIC-117 Rev J §1.2 defines the three signals that make up
the command interface (Step, Track Zero and Index); the
[QIC-117 page](../formats/qic-117.md) describes how they are used.

!!! note
    Tapewyrm is a recovery tool. It never asserts Write Enable, and the host
    refuses every QIC-117 command that writes or formats tape.

## USB link limits

The AT32F403A's USB controller is Full-Speed only. The firmware reports this
at runtime, and `tw info` shows it.

| Limit | Value | Consequence |
|---|---|---|
| Endpoint packet size | 64 bytes | A command response is sent as one packet. |
| Command response | 64 bytes, including the 2-byte header | A longer response is truncated, not split. Every Tapewyrm verb is sized to fit, or streams its data like a flux read. |
| Bulk throughput | About 1.2 MB/s theoretical | Enough for every standard data rate captured so far. |

The 64-byte response limit was found on the bench: a 253-byte `SCOPE`
response arrived cut off at 64 bytes and desynchronised the link. `SCOPE` now
caps its edge log at 10 entries (2 + 3 + 8 + 10 × 5 = 63 bytes).

Measured capture throughput during Logical Forward, from bench captures:

| Data rate | Average per capture set | Highest single track |
|---|---|---|
| 500 kbit/s | 392–396 kB/s | 412 kB/s |
| 1 Mbit/s | 610–637 kB/s | 676 kB/s |

The worst 1 Mbit/s track used about 56% of the theoretical ceiling. Two
things do not fit through the link: a 2 Mbit/s drive mode (about 1.3 MB/s by
scaling), and flux during high-speed physical motion. The firmware ends a
capture cleanly when its buffer overflows.

!!! note
    Share the USB bus as little as you can during a capture. The throughput
    figures above were measured with nothing else busy on the bus; a hub or a
    busy device on the same controller reduces the margin.

## What the Tapewyrm firmware adds

The Tapewyrm firmware is the Greaseweazle v1.6 firmware, vendored in-tree,
with QIC verbs added to its command loop. It reuses Greaseweazle's flux read
engine for captures and its pin primitives for the command channel. A stock
`gw` host still talks to the board, but sees the QIC verbs as unknown
commands.

| Verb | Number | Does |
|---|---|---|
| `CMD_QIC_INFO` | `0x80` | Device information and QIC capability gate: protocol version, capabilities, SRAM, sample clock. |
| `CMD_QIC_SET_TIMING` | `0x81` | Sets the QIC-117 pulse and report cadence from the drive profile. Idle only. |
| `CMD_QIC_PULSES` | `0x82` | Sends N Step pulses verbatim, then optionally clocks k report bits off Track Zero. |
| `CMD_QIC_WAIT_READY` | `0x83` | Polls the Index cue until the drive is ready or a timeout expires. |
| `CMD_QIC_CAPTURE` | `0x84` | Sends a motion command, then arms a free-running flux capture. |
| `CMD_QIC_SCOPE` | `0x87` | Bench logic probe: optional Step pulses, then an edge log of the drive's output lines. |
| `CMD_QIC_BUILD_INFO` | `0x88` | Firmware git commit and dirty flag, for `tw info`. |

The firmware knows nothing about what a command means. It sends the number of
pulses the host asks for, which keeps every QIC-117 decision on the host,
where it can be logged and changed without reflashing.

During a capture the firmware adds markers to Greaseweazle's flux stream: a
session start, a `SEGMENT` marker at each hardware Index edge, events, and an
end marker with counts and a checksum. The stream is saved verbatim as a TWRF
file ([TWS-1](../spec/twrf.md)). The wire protocol is in
[Device protocol](../dev/protocol.md), and the firmware internals are in
[Firmware](../dev/firmware.md).

## See also

- [Flash the Greaseweazle](../guide/flash-greaseweazle.md)
- [Wire up a drive](../guide/wiring.md)
- [Drives](drives/index.md)
