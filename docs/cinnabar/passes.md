# Passes and merged reads

A flux capture directory can hold several reads of one track. `cinnabar` lists
each track once and lets you pick the read with the **Pass** selector. A tape
image holds one merged read per track.

## Where several reads come from

`tw dump` reads each track once by default. Two options keep more:

- `--keep-all` keeps every pass instead of deleting the ones that lost, each
  as `track-NN.P.twrf`, where `P` is its `pass_id`.
- `--passes N` reads every track at least `N` times and keeps them all. It
  implies `--keep-all`.

`dump.jsonl`, the sidecar `tw dump` writes beside its captures, names the read
its rules picked for each track. See [tw dump](../tw/dump.md).

Opening any one capture of a dump opens all of them: the captures the sidecar
names, and every other `track-*.twrf` file in the same directory. Opening the
directory does the same.

## The Pass selector

The **Pass** selector sits on the toolbar beside the track selector and its
arrows. It lists the reads of the track on screen. The track list itself keeps
one entry per track, so stepping through tracks visits each track once.

| Entry | Meaning |
|---|---|
| `Pass 2` | The read whose capture header says `pass_id` 2. |
| `Pass 2 (best)` | The read `dump.jsonl` names for this track. Shown only when the track has more than one read. |
| `Pass 5 - track-05.5.twrf` | A `pass_id` that two or more reads share, told apart by file name. |
| `-` | Nothing to choose: an image's track, or a volume. |

Each entry's tooltip shows its file name. The selector is greyed out when the
track has only one read.

A `pass_id` does not always identify a read on its own. A `--retries` run's
`track-05.twrf` and a later `--keep-all` run's `track-05.5.twrf` can both be
pass 5, and every probe capture is pass 0. That is why shared numbers get the
file name.

A track opens on its `(best)` read. If `dump.jsonl` names none of the track's
reads, it opens on the first, and `cinnabar` logs that at INFO level.

## Open a specific read

Use `--pass` with `--track`:

```sh
cinnabar captures/NAME/ --track 5 --pass 3
```

| Value | Opens |
|---|---|
| `best` (default) | The read `dump.jsonl` names, or the first read if it names none. |
| A number | The read with that `pass_id`. If several reads share it, the first in the selector's order opens and a warning names the others; pick the one you want from the selector. |

`--pass` on a track with no passes to choose from, such as an image's track, is
a usage error. So is a `pass_id` the track does not have; the message lists the
ones it has.

## Switching reads

Only one capture track decodes at a time. When you switch to another track or
pass, decoding of the one you leave pauses, and it picks up where it stopped
when you come back. Switching while a read is still loading cancels that load
and starts the new one. See
[Background decoding](settings.md#background-decoding).

## Merged reads

`cinnabar` does not merge reads. `tw convert` does: it combines every read of
each track into one tape image, choosing at the grain you set with `--best`
(`sector`, the default, lets every read fill the others' gaps; `segment` or
`track` takes each unit whole from its best read, ranked by `--rank crc` or
`--rank rs`). See [tw convert](../tw/convert.md).

To see what the merge produced, open the image. It has exactly one read per
track, so the Pass selector shows `-`, and the track strip and the
`TWTI: Sectors` lane show each segment's state after the merge.

To find out which read held a sector the others lost, open the capture
directory and step through the Pass selector on that track and segment.

## Examples

Open a dump and step through the reads of track 5 in the window:

```sh
cinnabar captures/NAME/ --track 5
```

Print the lanes of one read as text:

```sh
cinnabar captures/NAME/ --track 5 --pass 3 --dump --segment 12
```

Compare with the merged result in the image:

```sh
cinnabar captures/NAME.twtz --track 5 --dump --segment 12
```

## See also

- [tw dump](../tw/dump.md)
- [tw convert](../tw/convert.md)
- [TWS-1 TWRF](../spec/twrf.md)
- [cinnabar](index.md)
