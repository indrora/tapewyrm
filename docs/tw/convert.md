# tw convert

Decodes TWRF captures into a TWTI or TWTZ logical tape image.

## Synopsis

```text
tw [GLOBAL OPTIONS] convert [OPTIONS] SOURCE... OUTPUT
```

## Description

`tw convert` is the second step of a recovery. It needs no hardware. Sources
come first and the image last, as with `cp`.

Each `SOURCE` is a dump directory, from which every `track-*.twrf` file is
used, or a single `track-NN.twrf` file. For each capture, `tw convert`:

1. Decodes it at the bit rate it was recorded at: flux to MFM to sectors.
2. Places each sector by the address in its own sector ID.
3. Merges every read of the same segment (see [Several reads](#several-reads)).
4. Applies the Reed-Solomon error correction to each segment.
5. Reads the header segment and the bad-sector map.

It then writes a TWTI image ([TWS-2 TWTI/TWTZ](../spec/twti.md)) holding
every segment of the tape and its recovery state.

**Image format.** The suffix of `OUTPUT` picks the format:

- `.twtz` writes the image compressed with zstd. It is small (a mostly blank
  1.75 GB QIC-3020 image is about 62 kB), and every `qicsilver` command reads
  it directly. Use it unless a tool needs random access.
- Any other suffix, normally `.twti`, writes the image uncompressed and
  sparse: unrecovered and blank segments take no disk space on file systems
  that support holes.

Readers recognise the format by its magic bytes, not its suffix.

**Time and memory.** One capture decodes on one core, in roughly a quarter of
the tape's running time: a 9-minute QIC-3020 track converts in about 2.5
minutes. `-j N` decodes N captures at once, each in its own process. `-j 0`
picks N from the cores and the RAM: a worker needs about 25 bytes of memory
per byte of capture (2.5 GB for a 100 MB track), and the pick leaves a quarter
of the RAM free. On one 14-core laptop a 40-track QIC-3010 dump took 22
minutes with one job and 3 minutes 9 seconds with `-j 0` (11 workers).

Ctrl-C stops the run. A capture that is not a TWRF file stops it once the
captures in flight have finished.

### Several reads

Several dumps of the same tape, or the extra passes of `tw dump --keep-all`
and `--passes`, give several reads of the same track. `--best` sets the grain
at which they compete:

| `--best` | Behaviour |
|---|---|
| `sector` (default) | Every CRC-good sector of every read is used. A sector read on any pass fills the gaps of the others, so reading only the bad tracks again is enough. |
| `segment` | Each segment comes whole from its best read. |
| `track` | Each track comes whole from its best read. |

With `segment` or `track`, `--rank` decides which read is best. `crc` counts
CRC-clean sectors. `rs` first asks whether the segment corrects and its spare
Reed-Solomon parity agrees, which catches a sector that passed its CRC but is
wrong, then counts CRC-clean sectors. `--rank` has no effect with
`--best sector`, and `tw` warns if you set it.

Where every read got a sector wrong, `tw convert` still tries to rebuild it,
unless you pass `--no-vote`:

- **Vote.** A sector read CRC-bad 3 or more times is majority-voted byte by
  byte. The result is kept only if its CRC then checks.
- **Column solve.** A segment that still will not correct is solved one
  Reed-Solomon column at a time from the bytes its reads agree on. The result
  is kept only if every column's spare parity checks.

## Options

| Option | Default | Description |
|---|---|---|
| `SOURCE...` | | One or more dump directories or `track-NN.twrf` files. |
| `OUTPUT` | | The image to write. `.twtz` compresses it. |
| `-j`, `--jobs N` | `1` | Decode N captures at once, each in its own process. `0` picks N from the cores and the RAM. |
| `--best` | `sector` | `track`, `segment` or `sector`: the grain at which several reads of one track compete. |
| `--rank` | `crc` | `crc` or `rs`: what makes a read best for `--best track` or `--best segment`. |
| `--no-vote` | off | Do not rebuild sectors and segments that no single read got right. |

`--progress` shows one bar counting finished captures and, under it, each
capture in flight with its own stage bars.

## Output

The log goes track by track. For each track it lists which files read it,
then what its segments came out as, what each read held and contributed, and
one line for every segment that needed a merge, a vote or a rescue, or failed.
A segment whose spare parity disagrees is logged as a WARNING, because a
sector that passed its CRC is wrong; a segment made of one repeated byte is
space never written since the tape was formatted, and is counted as
`unwritten (format fill)` instead.

The end of the log summarises the image:

```text
INFO     track-00.twrf: 46,860 sectors: 46,718 CRC-clean, 142 data-CRC bad, 21 ID-only (143.8 s)
INFO     merge: 1 pass, 46,860 sectors -> 46,844 unique; later passes added 0, fixed 0 CRC-bad
INFO     header: segment 0 (clean), tape name (none), format code 4
INFO     geometry: 40 tracks x 1475 segs/track = 59,000 segments
INFO     cartridge: QIC-3020, 1,000 ft x 0.250 in (MC3020EX class, Verbatim QIC-Extra) ...
INFO     segments: 1,445 clean, 12 corrected, 14 uncorrectable, 57,431 missing, 98 bad (map)
INFO     converted 1 capture(s) in 148.0 s total
```

(From a blank, factory-formatted QIC-Extra tape, track 0 only.)

| Segment state | Meaning |
|---|---|
| `clean` | Recovered with nothing to correct. |
| `corrected` | Repaired by the error correction. |
| `uncorrectable` | More bad sectors than the error correction can repair (3 per segment). Dump the track again and merge. |
| `missing` | No read covered it, usually because its track was not dumped. |
| `bad (map)` | Marked bad in the bad-sector map at format time. Never used, so not a loss. |

## Examples

Convert one dump to a compressed image, using every core the RAM allows:

```sh
tw --progress convert -j 0 captures/mytape captures/mytape.twtz
```

Merge a second dump of some tracks into the same image:

```sh
tw convert captures/mytape captures/mytape-pass2 captures/mytape.twtz
```

Take each segment whole from its best read, judged by the spare parity:

```sh
tw convert --best segment --rank rs captures/mytape captures/mytape.twtz
```

Write an uncompressed sparse image:

```sh
tw convert captures/mytape captures/mytape.twti
```

## Exit status

`0` when the image was written. `1` when a source holds no `track-*.twrf`
captures, a capture cannot be read, or the run was stopped.

## See also

- [tw dump](dump.md)
- [tw inspect](inspect.md)
- [qicsilver identify](../qicsilver/identify.md)
- [Passes and merged reads](../cinnabar/passes.md)
- [Error correction](../formats/ecc.md)
- [TWS-2 TWTI/TWTZ](../spec/twti.md)
