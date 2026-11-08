# Troubleshooting

This page lists the common failures, what causes them and what to do. Drive
quirks are described in full on each drive's page under
[Drives](../hardware/drives/index.md); this page gives the short version and
links there.

When something fails and the message does not say why, run the command again
with `-v` before the subcommand (`tw -v drive status`). Debug logging shows
every decision, guard and command, and which config file and profile were
picked up.

## The board does not answer

| Message | Cause | Fix |
|---|---|---|
| `no Greaseweazle found (USB 1209:4d69); pass --port` | No Greaseweazle on USB, or the OS has not created its serial port. | Check the USB cable. Run `tw --port PORT info` with the port name. |
| `several Greaseweazles found [...]; pass --port to pick one` | More than one board is attached. | Pass `--port PORT`, or set `port` in the [config file](../tw/config.md). |
| `device proto_ver 0 < required ... (refusing stock/old firmware)` | The board runs stock Greaseweazle firmware or an old Tapewyrm image. | [Flash the Greaseweazle](flash-greaseweazle.md). `tw info` still works and shows which firmware is running. |

## The drive does not answer

`tw` stops with `no drive answered auto-detection (tried: ...)`. None of the
wake-ups `auto` tried got an answer. Work down this list:

1. **Power.** Check that the drive gets both 5 V and 12 V from its own
   supply.
2. **Cable.** Check that the cable is pin 1 to pin 1 at both ends.
3. **Profile.** Read the `auto:` log lines. A drive that needs a profile
   `auto` never tries (`teac`, `conner`, `iomega`, or your own file) needs
   `--profile NAME`. `auto: skipping insight: ...` means the board refused the
   motor lines; the other wake-ups were still tried.
4. **Config file.** A `profile` in the config file replaces `auto`.
   `tw -v drive status` logs which file and profile were loaded.
5. **Unit address.** Try another phantom unit address:
   `tw --profile colorado drive select --unit 1`.
6. **Another drive is selected.** A phantom drive left selected by an earlier
   session can hold the bus. Run `tw drive deselect`, or switch the drives'
   power off and on.
7. **Look at the lines.** `tw --profile colorado drive scope` logs edges on
   TRK0 and INDEX. A selected, ready drive shows cue INDEX pulses every few
   milliseconds; no edges at all points at power, cabling or selection.

If a named profile (`--profile colorado`, `mountain` or `insight`) works where
`auto` does not, `auto` is at fault. Only the Colorado wake-up has been tested
on real drives. Name the profile and report the problem.

## The drive goes dark (TEAC FT-3010)

The TEAC stops answering on every line, and only a power cycle brings it back.
This happens when a report command is interrupted part-way, including an ACK
time-out or `tw drive scope --pulses N`. Use `--profile teac`, never `auto`,
and do not send it stray pulses. See
[TEAC FT-3010](../hardware/drives/teac-ft3010.md).

## The dump will not start

| Message | Fix |
|---|---|
| `before track N: drive not ready+referenced (...)` | Run `tw drive status`. If `referenced` is missing, run `tw drive load-point` and wait for it; it can take 30 seconds or more. |
| `the drive did not report the tape's format, so the track count is unknown` | The drive does not answer Report Tape Status, and its configuration report does not have the QIC-80 bit set. Name the tracks: `tw dump captures/mytape 0-27` for QIC-80. |
| `track N: ... not at physical BOT` (or `EOT`) | The wind to the track's start did not end at the end of the tape. Run `tw drive load-point`, then resume with `--no-clobber`. |

## The Colorado 1400 will not read a QIC-80 tape

The bench Colorado 1400 never references a QIC-80 tape, and refuses
`tw drive rate 500` and `tw drive format qic80` with error 31 (Rate or Format
Selection Error). Dump QIC-80 tapes on a QIC-80 drive such as the Jumbo 350.
See [Colorado 1400](../hardware/drives/colorado-1400.md).

## The dump stops part-way

`tw dump` prints `dump stopped: track N: REASON`. After every pass it checks
that the pass ended at the end of the tape, that it found at least 90% as many
segments as the best pass so far, and that it missed no more than 5% of its
segments. With `--check` it also stops when fewer than 80% of the sectors are
CRC-clean. Any of these usually means a dirty head or a failing tape.

1. Read the reason.
2. Clean the head ([A dirty head](#a-dirty-head)).
3. Resume in place: run the same command with `--no-clobber`. Tracks already
   dumped clean are skipped; the track that stopped is read again.
4. Add `--retries 2` so a flaky pass is read again before the health check
   judges it.

## No header found

`qicsilver identify` says `no header segment found: the capture must include
the start of track 0`, or `neither copy of the header segment was recovered`.

- Make sure the image includes track 0: `tw dump captures/mytape 0`, then
  convert again.
- Check the rate in `drive config` (`tw drive status`). A capture at the wrong
  rate decodes no sectors.
- Dump track 0 again into a new directory and convert both directories
  together.
- Record a short sample and count what decodes:

    ```sh
    tw --profile colorado drive flux --motion logical --seconds 10
    ```

    Zero sectors means a head, rate or tape problem, not a software one. Use
    `--motion logical`: the default `--motion fwd` runs the tape at high
    speed, where sectors never decode. See [tw drive](../tw/drive.md#tw-drive-flux).

## Wrong volume profile

The volume table reads as nonsense: dates in the wrong century, absurd sizes,
or `extract` failing on the first volume. The layout of a volume-table entry
depends on the backup program that wrote it, and `qicsilver` guessed wrong.

1. Run `qicsilver identify --raw captures/mytape.twtz` to see each profile's
   score.
2. Force the right one with `--volume-profile NAME` on `identify`, `extract`,
   `inspect` and `tar`.
3. If none fits, set the layout by hand with the override flags.

See [Cartridge and volume profiles](../qicsilver/profiles.md) and
[Manual read overrides](../qicsilver/overrides.md).

## A dirty head

Old tapes shed oxide onto the head. Signs:

- A dump that stops with fewer segments found than on earlier tracks.
- `--check` failing below 80%.
- A track that converts much worse than its neighbours.

Stop. Clean the head and the capstan with isopropyl alcohol and a lint-free
swab, and let them dry. Then resume the dump with `--no-clobber`, or dump the
remaining tracks into a new directory and convert the directories together.

`tw drive clean` runs a cleaning cartridge back and forth. It is untested on
the bench: the cleaning cartridge on hand has never tripped a test drive's
cartridge switch. See [Cartridges](../hardware/cartridges.md).

## Segments are uncorrectable after convert

`tw convert` reports `uncorrectable` segments: those had more bad sectors than
the error correction can repair (3 per segment).

- Dump the affected tracks again into a new directory and convert every
  directory together. A sector read on any pass fills the gaps of the others.
- Keep every read with `tw dump --keep-all` or `--passes N`. With three or
  more reads of a sector, `tw convert` majority-votes it byte by byte.
- Look at the damage in [cinnabar](../cinnabar/index.md) to see which tracks
  and segments are worth reading again.

See [tw convert](../tw/convert.md).

## See also

- [tw drive](../tw/drive.md)
- [Configuration and logging](../tw/config.md)
- [Drives](../hardware/drives/index.md)
