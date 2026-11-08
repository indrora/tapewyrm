# Read a tape

This page takes one cartridge from the drive to a tar file of its backup. It
uses `captures/mytape` as the working name; use any name, but keep the files
under `captures/` ([Keep captures private](privacy.md)).

Output marked **(example output)** shows the format the command prints with
plausible values, not a real session.

## Prerequisites

- The tools installed, with the `tw` and `qicsilver` aliases
  ([Install the tools](install.md)).
- The Greaseweazle flashed and the drive wired and powered
  ([Flash the Greaseweazle](flash-greaseweazle.md),
  [Wire up a drive](wiring.md)).
- A clean head and capstan. Old tapes shed oxide, and every pass over a
  failing tape costs something. Clean before a valuable tape goes in; see
  [A dirty head](troubleshooting.md#a-dirty-head).
- Free disk space. A capture is roughly 50 MB per track of a short QIC-80
  cartridge, and more for longer and faster tapes.

## Step 1: Check the board

Run `tw info`.

```sh
tw info
```

The `firmware` line should show a protocol version and a capability list. If
it says `stock Greaseweazle (no Tapewyrm verbs)`, flash the Tapewyrm firmware
first. With more than one Greaseweazle plugged in, pass `--port PORT` before
the subcommand.

## Step 2: Load the cartridge

Insert the cartridge and wait for the drive to finish its own load sequence.
The drive winds the tape to find its reference holes; this can take 30 seconds
or more.

## Step 3: Wake the drive

Run `tw drive select`.

```sh
tw drive select
```

```text
[12:00:00] INFO     auto: trying default: no wake steps
           INFO     auto: default: no answer (command 6: no ACK bit -- ...)
           INFO     auto: trying colorado: phantom select 0, enter primary mode
           INFO     auto: drive answered colorado; using profile 'colorado'
status : 0x25 [ready cartridge_present referenced]
select : yes -- cue INDEX every 2.9 ms
```

(Example output.) `auto` logs each wake-up it tries and the one the drive
answered. The rest of this page names the profile with `--profile colorado`;
use the one your drive answered, or the one its
[drive page](../hardware/drives/index.md) names. To stop typing it, set it in
the [config file](../tw/config.md).

## Step 4: Read the drive's reports

Run `tw drive status`.

```sh
tw --profile colorado drive status
```

```text
cleared on wake: 26 Power On Reset Occurred (process error)
drive status : 0x25 [ready cartridge_present referenced]
last error   : 0x001a -> 26 Power On Reset Occurred (process error)
drive config : 0x90 -> 500 kbps, QIC-80 bit set
rom version  : 0x5c -> version 92
vendor id    : not supported by this drive (no ACK)
tape status  : not supported by this drive (no ACK)
```

(Example output.) Check three things:

- **`ready` and `referenced`** in `drive status`. `referenced` means the
  drive has found the tape's reference holes. `tw dump` refuses to start
  without it. If it is missing, run `tw --profile colorado drive load-point`,
  which can take 30 seconds or more.
- **The rate** in `drive config`. `tw dump` records at the rate the drive
  reports. QIC-40 and QIC-80 tapes are 500 kbps, QIC-3010 is 1 Mbps and
  QIC-3020 is 2 Mbps.
- **`tape status`**, if the drive answers it. `tw dump` with no track list
  takes the track count from the tape format reported here.

Older drives do not answer every report. `not supported by this drive (no
ACK)` is normal for those. Each field is described in
[tw drive](../tw/drive.md#tw-drive-status).

!!! note
    The Colorado 1400 on the bench refuses the QIC-80 rate and format (error
    31) and has not read a QIC-80 tape. Dump QIC-80 tapes on a QIC-80 drive
    such as the Jumbo 350. See [Colorado 1400](../hardware/drives/colorado-1400.md).

## Step 5: Look at track 0 first

The header and the volume table are at the start of track 0, so one track is
enough to see what the tape holds and whether it reads at all.

```sh
tw --profile colorado --progress dump --check captures/mytape 0
```

`--check` also decodes the pass and stops if fewer than 80% of its sectors are
CRC-clean. Expect a few minutes for a short QIC-80 cartridge: the pass winds
to the track's start, then runs the tape end to end.

## Step 6: Dump the whole tape

Dump every track into the same directory. `--no-clobber` skips track 0, which
is already there.

```sh
tw --profile colorado --progress dump --retries 2 --no-clobber captures/mytape
```

```text
[12:00:01] INFO     drive: config 0x90 -> 500 kbps, tape QIC80; writing TWRF to captures/mytape
           INFO     no tracks named: dumping all 28 tracks of this QIC80 tape
           INFO     track  1: winding to its start...
[12:00:40] INFO     track  1: wound to its start in 38.9s
           INFO     track  1: capturing -> captures/mytape/track-01.twrf
[12:03:09] INFO     track  1: 47.6 MB in 149s; checking...
[12:03:14] INFO     track  1: END EOT, 147.3s of tape, 207 segments by INDEX, 0 missed in gaps
           ...
done: 27 tracks, 5589 segments by INDEX -> captures/mytape
```

(Example output.)

- With no track list, `tw dump` dumps every track of the format the drive
  reports: QIC-40 20, QIC-80 28, QIC-3010 and QIC-3020 40. If the drive does
  not report the format, the dump refuses to start; name the tracks, for
  example `0-27`.
- Each track is one pass, written to `captures/mytape/track-NN.twrf`.
  `captures/mytape/dump.jsonl` gets one line per pass.
- `--retries 2` reads a track up to two more times when its pass fails a
  health check, and keeps the best pass.
- After every pass `tw` checks, without decoding, that the pass ended at the
  end of the tape and found about as many segments as the best pass so far.
  If not, it stops rather than wear a failing tape further.
- A whole QIC-80 tape takes an hour or more. A track of a 1,000 ft QIC-3020
  cartridge takes about 9 minutes, plus winding.

If the dump stops with `dump stopped: track N: ...`, read the reason, clean
the head, and run the same command again: `--no-clobber` skips the tracks
already dumped clean and reads again the one it stopped on.

On a tape you expect to be damaged, keep every read instead of only the best:
`--passes 2 --retries 2`. All the options are in [tw dump](../tw/dump.md).

## Step 7: Make a tape image

Convert the captures to a TWTZ image. This step needs no hardware.

```sh
tw --progress convert -j 0 captures/mytape captures/mytape.twtz
```

Sources come first and the image last, as with `cp`. `tw convert` decodes
every capture, places each sector by its own address, applies the
Reed-Solomon error correction, and writes the image. `-j 0` decodes as many
captures at once as the cores and the RAM allow. Expect about a quarter of the
tape's running time with one job.

The last lines of the log summarise the result:

```text
INFO     header: segment 0 (clean), tape name (none), format code 4
INFO     geometry: 40 tracks x 1475 segs/track = 59,000 segments
INFO     segments: 1,445 clean, 12 corrected, 14 uncorrectable, 57,431 missing, 98 bad (map)
```

`missing` counts segments on tracks that were not dumped. `uncorrectable`
segments had more bad sectors than the error correction can repair. Dump those
tracks again into a new directory and convert both directories together:

```sh
tw --profile colorado dump captures/mytape-pass2 5,9
tw convert captures/mytape captures/mytape-pass2 captures/mytape.twtz
```

A sector read on any pass fills the gaps of the others. See
[tw convert](../tw/convert.md).

## Step 8: Identify the tape

```sh
qicsilver identify captures/mytape.twtz
```

`identify` prints the tape name, the cartridge type, the format and
last-written dates, the bad-sector map, and the volume table: one row per
backup volume, with its number, date, size, compression and description. Note
the volume numbers. If the volume table reads as nonsense, see
[Wrong volume profile](troubleshooting.md#wrong-volume-profile). Every field
is described in [qicsilver identify](../qicsilver/identify.md).

## Step 9: Extract the volumes

```sh
qicsilver --progress extract captures/mytape.twtz captures/mytape-vols
```

`extract` writes one `captures/mytape-vols/vol-NN.twvl` per volume, where `NN`
is the volume's number in `identify`. Bytes from segments that were not
recovered become recorded holes, so nothing after a hole shifts. See
[qicsilver extract](../qicsilver/extract.md).

List a volume before you write a tar:

```sh
qicsilver inspect captures/mytape-vols/vol-00.twvl
```

See [qicsilver inspect](../qicsilver/inspect.md).

## Step 10: Write the tar

```sh
qicsilver --progress tar captures/mytape-vols/vol-00.twvl captures/mytape.tar
```

```text
captures/mytape.tar: 2000 files, 150 directories, 12 damaged (report: captures/mytape.tar.damaged.txt)
```

(Example output.) The tar keeps the backup's paths, modification times and
DOS attributes. The damage report beside it lists every file that lost bytes
(`lost`) or that the original backup software could not read (`error`). A
`lost` file can come back if you dump its tracks again and merge; an `error`
file cannot. See [qicsilver tar](../qicsilver/tar.md).

`tar` and `inspect` also take the image directly, without `extract`:
`qicsilver tar captures/mytape.twtz captures/mytape.tar --volume 0`.

## Step 11: Release the drive

Phantom drives stay selected after `tw` exits. Release the drive before you
switch to another one on the same cable:

```sh
tw drive deselect
```

## Next steps

- [Troubleshooting](troubleshooting.md).
- Look at the captures and the image in [cinnabar](../cinnabar/index.md), to
  see which segments are damaged and which read contributed what.
- [Manual read overrides](../qicsilver/overrides.md), for a tape no volume
  profile fits.
