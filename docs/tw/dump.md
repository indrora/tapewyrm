# tw dump

Captures whole tape tracks to TWRF flux files, one Logical Forward pass per
track.

## Synopsis

```text
tw [GLOBAL OPTIONS] dump [OPTIONS] OUTDIR [TRACKS]
```

## Description

`tw dump` is the first step of a recovery. It reads every requested track as
raw flux and streams it to `OUTDIR/track-NN.twrf`. Each TWRF file records the
flux, the drive's identity and reports, and the bit rate, so it can be decoded
later without the hardware. `tw dump` does not decode the data; `tw convert`
does that. The file format is [TWS-1 TWRF](../spec/twrf.md).

**Tracks.** `TRACKS` is a list of tracks and ranges, each 0 to 63: `0`,
`0-27`, `0,3,5-7`. Without it, `tw dump` dumps every track of the tape,
counted from the format the drive reports: QIC-40 has 20 tracks, QIC-80 28,
QIC-3010 and QIC-3020 40. On wide (0.315 in) cartridges QIC-80 has 36 and
QIC-3010 and QIC-3020 have 50. If the drive cannot say
what the format is, the dump refuses to start and asks for `TRACKS`.

**Before each pass** `tw` checks that the drive is ready and referenced, and
winds the tape to the end where the track starts. Even tracks run toward the
end of the tape and start at the beginning; odd tracks run back and start at
the end. In ascending order each pass starts near where the last one ended, so
dump tracks in ascending order to avoid rewinds. Logical Forward starts
reading wherever the tape is, so the wind matters: without it a pass loses the
first segments of the track.

**After each pass** `tw` checks, without decoding, that the pass is healthy.
It stops the dump if any of these fail:

- The pass ended at the end of the tape (logical EOT).
- The capture stream verified.
- The drive found at least 90% as many segments (INDEX pulses) as on the best
  pass so far.
- No more than 5% of the segments were missed inside the pass (long gaps
  between INDEX pulses).
- With `--check`: at least 80% of the decoded sectors are CRC-clean.

Old tape can shed oxide, and every pass over a failing tape costs something,
so a failing check stops the dump for a human to look. The message is `dump
stopped: track N: REASON`.

**Retries and kept passes.** `--retries N` reads a failed track again, up to N
more times, before the checks judge it, and keeps the best pass. Normally the
losing passes are deleted. `--keep-all` keeps every pass, each as
`track-NN.P.twrf` where `P` is its pass number. A pass that loses as a whole
can still hold a sector the winner lost, and `tw convert` uses every capture
in the directory. `--passes N` reads every track at least N times whatever
happens, keeping them all; `--retries` then adds more only if none of the N
was clean. On a dying tape, `--passes 2 --retries 2` keeps every read.

**Resuming.** `--no-clobber` resumes a dump that stopped. A track whose last
`dump.jsonl` line is clean and whose file is whole is skipped. A track dumped
with a failed pass is read again, and its earlier capture is replaced only if
the new pass is better.

**Time.** A pass takes as long as the tape takes to run end to end: a couple
of minutes per track for a short QIC-80 cartridge, about 9 minutes for a track
of a 1,000 ft QIC-3020 cartridge, plus winding. A whole QIC-80 tape takes an
hour or more.

## Options

| Option | Default | Description |
|---|---|---|
| `OUTDIR` | | The directory to write into. Created if it does not exist. |
| `TRACKS` | every track of the tape | Tracks and ranges to dump, such as `0-12` or `0,2,5-7`. |
| `--check` | off | Also decode each pass, and stop if fewer than 80% of its sectors are CRC-clean. Adds about 20 seconds per track. |
| `--retries N` | `0` | Read a track up to N more times when its pass failed: the capture raised a link or drive error, the pass did not end at EOT, the stream did not verify, the drive found fewer segments than the best track so far, or any segment was missed inside the pass. The best pass is kept. |
| `--passes N` | `1` | Read every track at least N times whatever the outcome, and keep them all as `track-NN.P.twrf`. Implies `--keep-all`. |
| `--keep-all` | off | Keep every pass instead of deleting the ones that lost, each as `track-NN.P.twrf`. `dump.jsonl` still names the best one, and the stop rules judge that one. |
| `--no-clobber` | off | Resume a dump of `OUTDIR`: skip tracks already dumped clean, read again tracks dumped with a failed pass. |

The global `--profile` and `--port` select the drive and the board.
`--progress` shows bars for tracks, bytes and segments.

## Output

| File | Contents |
|---|---|
| `OUTDIR/track-NN.twrf` | The kept pass of track `NN`. |
| `OUTDIR/track-NN.P.twrf` | Pass `P` of track `NN`, with `--keep-all` or `--passes`. |
| `OUTDIR/dump.jsonl` | One JSON line per pass: which track, which file, how it ended, the segment counts, and why a pass was kept or thrown away. |

A re-read in progress is written beside the kept file with a `.retry` suffix,
so a run that dies part-way leaves nothing `tw convert` would pick up by
mistake.

On stdout, the command ends with a summary:

```text
done: 28 tracks, 5796 segments by INDEX -> captures/mytape
```

(Example output.) With `--check` it adds the CRC-clean sector count, and it
notes how many tracks were re-read and whether every pass was kept.

## Examples

Dump track 0 only and check it decodes, as a first look at a tape:

```sh
tw --profile colorado dump --check captures/mytape 0
```

Dump the whole tape with progress bars, reading failed tracks up to twice
more:

```sh
tw --profile colorado --progress dump --retries 2 captures/mytape
```

Resume that dump after it stopped:

```sh
tw --profile colorado --progress dump --retries 2 --no-clobber captures/mytape
```

Read every track twice and keep every read, for a damaged tape:

```sh
tw --profile colorado --progress dump --passes 2 --retries 2 captures/mytape
```

Dump only some tracks again, into a new directory, to merge later:

```sh
tw --profile colorado dump captures/mytape-pass2 5-7,12
```

## Exit status

`0` when every requested track was dumped. `1` when the dump stopped (`dump
stopped: ...`), the drive was not ready and referenced, or the board or drive
failed. Captures already written stay on disk.

## See also

- [tw convert](convert.md)
- [Read a tape](../guide/read-a-tape.md)
- [Passes and merged reads](../cinnabar/passes.md)
- [TWS-1 TWRF](../spec/twrf.md)
- [Troubleshooting](../guide/troubleshooting.md#the-dump-stops-part-way)
