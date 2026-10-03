# Quick start

From a QIC-117 floppy-tape drive on the bench to a tar file of the backup.
This assumes a Greaseweazle v4.1 and a drive Tapewyrm has a profile for (the
Colorado Jumbo 350 and Colorado 1400 are tested). See the
[root README](../README.md) for what Tapewyrm is.

```
tape --tw dump--> TWRF --tw convert--> TWTI/TWTZ --qicsilver extract--> TWVL --qicsilver tar--> .tar
```

Hardware output below is marked **(example output)**: it shows the format
`tw` prints, with plausible values, not a real session. Output from
`qicsilver identify` is real, from a blank (factory-formatted, never used)
QIC-Extra tape.

## 1. Prerequisites

- **[uv](https://docs.astral.sh/uv/)**. Every command below runs from the
  repository root with `uv run --project ...`; uv installs the dependencies on
  first use. Python 3.11 or newer.
- **[just](https://github.com/casey/just)** (optional). `just tapewyrm ARGS`
  is `tw ARGS` and `just qicsilver ARGS` is `qicsilver ARGS`.
- **Tapewyrm firmware on the Greaseweazle.** Stock Greaseweazle firmware has
  no QIC verbs. Build it (`just fw`, or by hand as in
  [firmware/README.md](../firmware/README.md#building)) and flash it:
  - `tw flash IMAGE` (`just flash`) updates a board that is already running
    Greaseweazle or Tapewyrm firmware, through its bootloader over USB.
  - `tw dfu IMAGE` (`just dfu`) is for the first flash or a recovery: strap
    the board's DFU header so the AT32 ROM bootloader starts, then run it.
    It needs `dfu-util` (`--dfu-util PATH` if it is not on your `PATH`).

To save typing, define two aliases for this shell:

```sh
alias tw='uv run --project packages/tapewyrm-cli tw'
alias qicsilver='uv run --project packages/qicsilver qicsilver'
```

## 2. Wiring and drive setup

In brief (the board's details are in [firmware/README.md](../firmware/README.md)
and [DESIGN.md §3](DESIGN.md)):

1. Connect the drive to the Greaseweazle's 34-pin floppy header with an
   ordinary floppy cable, pin 1 to pin 1. One drive on the cable is simplest.
2. Power the drive from a **separate PC power supply** (5 V and 12 V on the
   4-pin connector). The Greaseweazle cannot power a tape drive.
3. Connect the Greaseweazle to the host over USB.
4. Clean the head and the capstan before a valuable tape goes in (see
   [troubleshooting](#a-dirty-head)). Insert the cartridge and let the drive
   finish its own load sequence (it winds the tape end to end).

## 3. Is the board there? `tw info`

`tw info` talks to the Greaseweazle only, not the drive:

```console
$ tw info
tw        : 0.1.0, commit 5ed5c63a1b2c [git checkout]
device    : tapewyrm-GW V4.1
port      : /dev/cu.usbmodem1101
mcu       : 288 MHz, 224 kB SRAM
firmware  : 1.6, commit 5ed5c63a1b2c, protocol v1 [capture, markers, verbs]
```
(example output)

If `firmware` says `stock Greaseweazle (no Tapewyrm verbs)`, flash the
Tapewyrm firmware first. With several boards plugged in, pass
`tw --port PORT ...`.

## 4. Wake the drive: `tw drive select` and `tw drive status`

`tw` sends a **drive profile**'s wake sequence before every drive command.
Choose the profile with the global `--profile` flag, before the subcommand:

| `--profile` | Drive |
|---|---|
| `auto` (the default) | Tries the wake-ups the Linux ftape driver tries, in ftape's order, and keeps the first the drive answers. See below. |
| `colorado` | Colorado Jumbo 350 (and family). Tested. |
| `colorado.1400` | Colorado 1400 (QIC-3010). Tested. |
| `mountain` | ftape's "Mountain" wake (Soft Select): Conner, Archive, Summit. Untested. |
| `insight` | ftape's "Motor-on" wake: Irwin/Insight 80, early Iomega. Untested. |
| `conner`, `iomega` | Untested placeholders (Soft Reset wake; not from ftape). |
| `default` | No wake sequence at all (ftape's "None"). |

The profiles live in
[`packages/tapewyrm-cli/tapewyrm/profiles/drive/`](../packages/tapewyrm-cli/tapewyrm/profiles/drive/);
`--profile path/to/my-drive.toml` loads your own.

**`auto`**, used when no profile is named anywhere, does what the Linux
floppy-tape driver ftape does when it does not know the drive yet
(`ftape_activate_drive()` in ftape-ctl.c, Linux 2.6.19): it tries ftape's four
wake-up methods in ftape's order and keeps the first one the drive answers.
"Answers" is ftape's test: Report Drive Status succeeds within 4 tries and is
not 0xff. ftape's behaviour is the safety precedent: these are the wakes it
sent, unasked, to any drive on the cable.

| Try | Profile | ftape method | What `tw` sends |
|---|---|---|---|
| 1 | `default` | None | nothing; just Report Drive Status |
| 2 | `colorado` | Colorado | Phantom Select (46) + unit 0, then Enter Primary Mode (30) |
| 3 | `mountain` | Mountain | Soft Select (23) + its 20-pulse train |
| 4 | `insight` | Motor-on | wait 100 ms, then the drive-select and motor-enable lines of unit 0 |

Like ftape, `tw` undoes nothing between tries except the motor: after a
Motor-on try that gets no answer it switches the motor off and deselects. It
also switches that motor off at the end of every session. If no drive
answers, it stops with an error. Each try and its result is logged:

```console
$ tw drive select
[12:00:00] INFO     auto: trying default: no wake steps
           INFO     auto: default: no answer (command 6: no ACK bit -- ...)
           INFO     auto: trying colorado: phantom select 0, enter primary mode
           INFO     auto: drive answered colorado; using profile 'colorado'
status : 0x25 [ready cartridge_present referenced]
select : yes -- cue INDEX every 2.9 ms
```
(example output)

A phantom drive that an earlier session left selected answers try 1, so
the second `tw` command after a cold start usually reports `using profile
'default'`. That is expected (ftape does the same); `default` has the same
timings. A Colorado 1400 is picked up as `colorado`, which has the same wake
and timings as `colorado.1400`. `conner` and `iomega` are never tried
automatically: ftape does not wake drives with a Soft Reset, and a Soft Reset
deselects a phantom drive. Name them with `--profile`.

**Not yet checked on hardware:** `auto` as a whole. Only the Colorado wake
has met real drives (the 350 and the 1400). The None, Mountain and Motor-on
tries, the order, the 4-try status test and the Motor-on undo have only run
against simulated drives. Motor-on drives the Greaseweazle's IBM PC bus unit
0 lines (cable pins 14 and 10); that this matches what a PC floppy controller
does under ftape is our reading, not tested. If `auto` misbehaves, name the
profile with `--profile NAME` and report it.

**Set it once.** Instead of passing `--profile` every time, put it in the
per-user config file, which `tw` reads when `--config` is not given:
`~/.config/tapewyrm/config.toml` (or `$XDG_CONFIG_HOME/tapewyrm/config.toml`;
on Windows `%APPDATA%\tapewyrm\config.toml`):

```toml
profile = "colorado.1400"
# port = "/dev/cu.usbmodem1234"   # optional, as --port
```

`--profile` on the command line still wins over the file, and `--config FILE`
reads that file instead of the per-user one.

**Colorado drives are "phantom" drives.** They ignore the floppy drive-select
lines and are selected by a QIC-117 command instead: Phantom Select (command
46) followed by a unit-address argument (unit 0 for the Jumbo 350 and the
1400). The `colorado` profiles (and `auto`'s second try) do this. A drive selected that
way **stays selected** after `tw` exits, until `tw drive deselect`, a reset,
or a power cycle. `tw drive select --unit N` tries another unit address.

A selected, ready drive pulses INDEX every few milliseconds; seeing that is
the proof it is listening. Then read everything the drive reports:

```console
$ tw --profile colorado drive status
cleared on wake: 26 Power On Reset Occurred (process error)
drive status : 0x25 [ready cartridge_present referenced]
last error   : 0x001a -> 26 Power On Reset Occurred (process error)
drive config : 0x90 -> 500 kbps, QIC-80 mode
rom version  : 0x5c -> version 92
vendor id    : not supported by this drive (no ACK)
tape status  : not supported by this drive (no ACK)
```
(example output)

Look for `ready` and `referenced` (the drive has found the tape's reference
holes; `tw dump` refuses to start without it) and for the rate in
`drive config`: `tw dump` records at that rate. The bench QIC-80 tapes read at
500 kbps; the Colorado 1400 reports 1 Mbps, its QIC-3010 rate, and may need
`tw drive rate 500` for a QIC-80 tape. Older drives don't answer every report; that is normal. If the
cartridge is not referenced yet, `tw --profile colorado drive load-point`
(which can take 30 s or more) has the drive find it again.

## 5. Dump the tape: `tw dump`

```sh
tw --profile colorado --progress dump captures/NAME
```

- The synopsis is `tw dump OUTDIR [TRACKS]`. With no TRACKS it dumps every
  track of the tape, counted from the format the drive reports: a QIC-40 tape
  has 20 tracks, QIC-80 28, QIC-3010 and QIC-3020 40 (wide cartridges: 36 and
  50). If the drive can't report the format, the dump refuses to start and
  asks for TRACKS. TRACKS takes ranges and lists: `0-27`, `0`, `0,3,5-7`.
- Each track is one **Logical Forward** pass, streamed to
  `captures/NAME/track-NN.twrf`. A TWRF file records the raw flux plus the
  drive's identity and the bit rate, so it can be decoded later without
  guessing. `captures/NAME/dump.jsonl` gets one line per pass.
- **Serpentine order.** Even tracks run toward the end of the tape, odd tracks
  back toward the beginning, so in ascending order each pass starts near where
  the last one ended. Before each pass `tw` winds to that track's starting end
  (Logical Forward starts reading wherever the tape is, and an earlier version
  lost the first segments of every track that way).
- **Health checks.** After every pass `tw` checks, without decoding, that the
  pass ended cleanly, that the drive found about as many segments as on the
  best pass so far (90%), and that few segments were skipped. If not, it stops:
  old tape can shed oxide, and every pass over a failing tape costs something.
- **`--check`** also decodes each pass (about 20 s more per track) and stops if
  fewer than 80% of its sectors are CRC-clean. Use it on a tape you don't
  trust yet, or on track 0 alone (`tw dump captures/NAME 0`) as a first look.
- **How long.** A pass takes as long as the tape takes to run end to end at the
  drive's speed: a couple of minutes per track for a short QIC-80 cartridge, and
  about 9 minutes for a track of a 1,000 ft QIC-3020 cartridge, plus winding.
  A whole QIC-80 tape is an hour or more.

```console
$ tw --profile colorado dump captures/NAME 0-1
[12:00:01] INFO     drive: config 0x90 -> 500 kbps, tape QIC80; writing TWRF to captures/NAME
           INFO     track  0: winding to its start...
[12:00:40] INFO     track  0: wound to its start in 38.9s
           INFO     track  0: capturing -> captures/NAME/track-00.twrf
[12:03:09] INFO     track  0: 47.6 MB in 149s; checking...
[12:03:14] INFO     track  0: END EOT, 147.3s of tape, 207 segments by INDEX, 0 missed in gaps
           ...
done: 2 tracks, 414 segments by INDEX -> captures/NAME
```
(example output)

If the dump stops (`dump stopped: track N: ...`), read the reason, clean the
head, and dump the rest into a **new** directory, e.g.
`tw dump captures/NAME-pass2 5-27`. `tw convert` merges them.

## 6. Make a tape image: `tw convert`

```sh
tw --progress convert captures/NAME captures/NAME.twtz
```

The synopsis is `tw convert SOURCE... OUTPUT`: sources first, the image last,
as with `cp`. No hardware is needed. `tw convert` decodes every capture at the rate it
recorded (flux -> MFM -> sectors), places each sector by its own address,
applies the Reed-Solomon ECC, reads the header and bad-sector map, and writes
a **TWTI** logical tape image. It takes roughly a quarter of the tape's running
time: a 9-minute QIC-3020 track converts in about 2.5 minutes.

- **TWTZ or TWTI.** The file name picks the format. `.twtz` is the image
  compressed with zstd: small (a mostly blank 1.75 GB QIC-3020 image is 62 kB),
  and every `qicsilver` command reads it directly. `.twti` is uncompressed and
  written sparse, so unrecovered or blank segments take no disk space on file
  systems that support holes. Use `.twtz` unless a tool needs random access.
- **Merging dumps.** Give several sources to merge them:
  `tw convert captures/NAME captures/NAME-pass2 captures/NAME.twtz`. Sources can be
  dump directories or single `track-NN.twrf` files. A sector read on any pass
  fills the gaps of the others, so re-reading only the bad tracks is enough.

The log says how it went. From the blank QIC-Extra tape (track 0 only):

```text
INFO     track-00.twrf: 46,860 sectors: 46,718 CRC-clean, 142 data-CRC bad, 21 ID-only (143.8 s)
INFO     merge: 1 pass, 46,860 sectors -> 46,844 unique; later passes added 0, fixed 0 CRC-bad
INFO     header: segment 0 (clean), tape name (none), format code 4
INFO     geometry: 40 tracks x 1475 segs/track = 59,000 segments
INFO     cartridge: QIC-3020, 1,000 ft x 0.250 in (MC3020EX class, Verbatim QIC-Extra) ...
INFO     segments: 1,445 clean, 12 corrected, 14 uncorrectable, 57,431 missing, 98 bad (map)
INFO     converted 1 capture(s) in 148.0 s total
```

`missing` is every segment on the tracks that were not dumped;
`uncorrectable` segments had more bad sectors than the ECC can repair (3 per
segment). Dump those tracks again and merge.

## 7. What is on it: `qicsilver identify`

```console
$ qicsilver identify captures/NAME.twtz
tape name     -  (named 1999-11-21 12:30:02)
manufacturer  FMTJ  (lot -)
              factory pre-formatted (QIC-3020-MC Rev H §7.1 bytes 146-233)
cartridge     QIC-3020, 1,000 ft x 0.250 in (MC3020EX class, Verbatim QIC-Extra) 900 Oe, 1.7 GB native; 1475 segments/track ~ 1,030.4 ft; QIC-3020 per the drive's tape status
drive saw     QIC3020, variable length 900 Oe [tape status 0x63]; 1000 kbps, extra-length tape [config 0xd8]; drive Colorado Memory Systems
format        code 4, variable length (QIC-3020-MC Rev H); header revision 0x00 (unused in QIC-3020-MC Rev H)
geometry      40 tracks x 1475 segments = 59000 segments; floppy sides 0-57, tracks 0-254, sectors 1-128
data area     segments 2-58999
formatted     1999-11-21 12:30:02  (first 1999-11-21 12:30:02, 1 times)
last written  1999-11-21 12:30:02
lifetime      118003 segments written
bad sectors   35 sectors + 98 whole segments in the map
header        segment 0 (clean); copies at 0 and 1
volume table  segment 2 (clean); 0 volume(s)
```

Reading it:

- **tape name, manufacturer**: the name the owner gave the tape, and the
  factory stamp if the cartridge was sold pre-formatted (`no factory stamp`
  means its owner formatted it).
- **cartridge**: the cartridge type, from the cartridge profile that fits the
  header's tracks x segments (here, the QIC-Extra profile `qic-extra`).
- **drive saw**: what the drive reported while dumping (from the TWRF headers).
- **formatted, last written**: dates from the header. The format date is the
  factory date for a pre-formatted tape.
- **bad sectors**: the bad-sector map written at format time. Those sectors
  were never used, so they are not losses.
- **header, volume table**: where they were found and whether they were
  recovered `clean`, `corrected`, or not at all. This blank tape has no
  volumes. A tape with backups also prints a `volume profile` line and a
  table with one row per volume: number, segment range, date, size,
  compression (`no`, `QIC-122`) and the description.

Only track 0 is needed: the header and volume table are at its start, so
`tw dump captures/NAME 0` is enough to identify a tape.

Options:

- `--volume-profile NAME|PATH`: the volume table entry's layout depends on the
  backup program that wrote it. By default (`guess`) every volume profile is
  scored against the table and the best one is used. Force one if the guess is
  wrong: `qic80-rev-n` (the QIC-80 standard), `cms-qic113` (Colorado backup),
  `mtn`, `vendor-unknown`, or a path to your own TOML. `--raw` shows the
  scoring and the raw records.
- `--json`: everything above as JSON (`header`, `cartridge`, `drive`,
  `bad_sectors`, `volume_profile`, `volume_profile_scores`, `volumes`, ...),
  for scripts.

## 8. Get the files: `qicsilver extract` and `qicsilver tar`

```sh
qicsilver --progress extract captures/NAME.twtz captures/NAME-vols
qicsilver --progress tar captures/NAME-vols/vol-00.twvl captures/NAME.tar
```

The synopses are `qicsilver extract IMAGE OUTDIR` and `qicsilver tar VOLUME
OUTPUT`: input first, output last. `extract` writes one `vol-NN.twvl` per volume in the volume table and prints
their paths. It decompresses QIC-122 data and lays each volume out by its
QIC-113 offsets; bytes from segments that were not recovered become recorded
holes, so nothing after a hole is shifted. It takes `--volume-profile` like
`identify`.

`tar` turns one volume into a POSIX (pax) tar with the backup's own paths
(including long Windows 95 names), modification times and DOS attributes:

```console
$ qicsilver tar captures/NAME-vols/vol-00.twvl captures/NAME.tar
captures/NAME.tar: 2000 files, 150 directories, 12 damaged (report: captures/NAME.tar.damaged.txt)
```
(example output)

The **damage report** (`captures/NAME.tar.damaged.txt`, or `--report PATH`) lists the
lost tape segments and one line per damaged file: missing bytes, size, flag,
path. The flag is one of:

- **`lost`**: some or all of the file's bytes were in tape segments that could
  not be recovered. The tar holds those bytes as zeros; `--skip-damaged` leaves
  such files out instead. Dumping the bad tracks again and merging can bring
  them back.
- **`error`**: the original backup software could not read the file when the
  backup was made (the QIC-113 "file error" bit; often a file that was open or
  locked). The tape never had its contents, so no re-read will help.

## 9. Progress and logging

Both tools take the same global flags, placed before the subcommand:

- `--progress`: progress bars on stderr (tracks, bytes, segments).
- `-v`: debug logging: every decision, guard and command. Use it when
  something fails and you want to know why.
- `-q` / `-qq`: warnings only / errors only.

Results go to stdout and logs to stderr, so `qicsilver identify --json IMG >
captures/info.json` stays clean.

## 10. Troubleshooting

### The drive does not answer

- `tw info` first: if the board does not answer, check USB and `--port`.
- Read the `auto:` log lines. `no drive answered auto-detection` means none
  of ftape's four wake-ups got an answer: check the points below. A drive
  that needs something else (`conner`, `iomega`, your own TOML) needs
  `--profile NAME` (or `profile = ...` in `~/.config/tapewyrm/config.toml`).
  `auto: skipping insight: ...` means the board refused the motor lines; the
  other three were still tried.
- Does `--profile colorado` (or `mountain`, `insight`) behave differently from
  the default? It should not; if it does, `auto` is at fault (it is not yet
  checked on hardware): name the profile and report it.
- A config file can override the default: `tw -v drive status` logs which
  config file and profile were picked up.
- Is the drive powered (5 V and 12 V), and is the cable the right way round?
- Try other unit addresses: `tw --profile colorado drive select --unit 1`.
- Another phantom drive on the same cable may still be selected:
  `tw --profile colorado drive deselect`, or power-cycle the drives.
- `tw --profile colorado drive scope` logs the TRK0 and INDEX lines; a
  selected, ready drive shows cue INDEX pulses every few milliseconds.
- `tw dump` refuses to start until the drive is `ready` and `referenced`:
  run `tw drive status`, then `tw drive load-point`.

### No header found

`qicsilver identify` says `no header segment found: the capture must include
the start of track 0`, or `neither copy of the header segment was recovered`.

- The image must include track 0: `tw dump DIR 0` and convert again.
- The rate may be wrong. Check `drive config` in `tw drive status`. A QIC-3010
  drive reading a QIC-80 tape may need `tw drive rate 500` after the tape is
  referenced.
- Dump track 0 again into a new directory and merge both dumps.
- `tw drive flux --seconds 10` records a short sample and reports how much
  signal and how many sectors it sees. Zero sectors means a head, rate or
  tape problem, not a software one.

### Wrong volume profile

The volume table reads as nonsense: dates in the wrong century, absurd sizes,
or `extract` failing on the first volume. Run
`qicsilver identify --raw IMG` to see each profile's score, then force the
right one with `--volume-profile NAME` on both `identify` and `extract`. If
none fits, the tape came from backup software Tapewyrm does not know yet: write
a new profile in `packages/qiclib/qiclib/profiles/volume/` (the existing files
explain their offsets) and pass its path.

### A dirty head

Old tapes shed oxide onto the head. Signs: a dump that stops with fewer
segments found than on earlier tracks, `--check` failing below 80%, or a track
that converts much worse than its neighbours. Stop, clean the head (and the
capstan) with isopropyl alcohol and a lint-free swab, let it dry, and dump the
remaining tracks into a new directory. Merge the dumps with `tw convert`.

## Keep captures private

The bench tapes are **strangers' backups**, bought from a PC recycler. They
hold other people's documents, mail and accounts. Treat every capture as
personal data:

- **Never publish** captures, images, volumes, tars or damage reports, or
  anything from inside them: file names, tape labels, directory listings or
  file contents. Not in issues, test fixtures, screenshots or logs.
- Keep them in `captures/`, which is gitignored. `.twrf`, `.twti`, `.twtz`
  and `.twvl` files are ignored anywhere in the tree, but tars and damage
  reports are not, so write those into `captures/` too. Never `git add -f`
  anything from it.
- Tests use synthetic tapes from `qiclib.testing`, or a blank tape like the one
  above, never a real backup.
