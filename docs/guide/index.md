# Guide

These pages take you from a QIC-117 floppy-tape drive on the bench to a tar
file of the backup on the tape. They assume a Greaseweazle v4.1 and a drive
Tapewyrm has a profile for. The Colorado Jumbo 350, the Colorado 1400 and the
TEAC FT-3010 have been tested; see [Drives](../hardware/drives/index.md).

Work through them in order the first time:

1. [Install the tools](install.md): `uv`, the repository, and the three
   commands `tw`, `qicsilver` and `cinnabar`.
2. [Flash the Greaseweazle](flash-greaseweazle.md) with the Tapewyrm firmware.
3. [Wire up a drive](wiring.md): cable, power and drive selection.
4. [Read a tape](read-a-tape.md): dump, convert, identify, extract, tar.
5. [Troubleshooting](troubleshooting.md), when a step does not do what the
   page says.
6. [Keep captures private](privacy.md). Read this before you dump a tape you
   did not write yourself.

## The pipeline

A tape passes through three file formats on its way to a tar file. Each
format is the complete hand-off to the next step, so you can stop after any
step and continue later, on another machine, without the hardware.

```text
 tape in drive
   |
   |  tw dump            (hardware: Greaseweazle + drive)
   v
 captures/mytape/track-NN.twrf     TWRF: raw flux, one file per track pass
   |
   |  tw convert         (no hardware)
   v
 captures/mytape.twtz              TWTI/TWTZ: logical tape image, every segment
   |
   |  qicsilver identify (what is on it)
   |  qicsilver extract
   v
 captures/mytape-vols/vol-NN.twvl  TWVL: one backup volume, decompressed
   |
   |  qicsilver tar
   v
 captures/mytape.tar               pax tar of the files
 captures/mytape.tar.damaged.txt   damage report: what could not be recovered
```

| Step | Command | Input | Output | Reference |
|---|---|---|---|---|
| Capture | `tw dump` | the tape | TWRF captures and `dump.jsonl` | [tw dump](../tw/dump.md), [TWS-1 TWRF](../spec/twrf.md) |
| Decode | `tw convert` | TWRF captures | TWTI (`.twti`) or TWTZ (`.twtz`) image | [tw convert](../tw/convert.md), [TWS-2 TWTI/TWTZ](../spec/twti.md) |
| Identify | `qicsilver identify` | image | a summary of the header and volume table | [qicsilver identify](../qicsilver/identify.md) |
| Extract | `qicsilver extract` | image | one TWVL file per volume | [qicsilver extract](../qicsilver/extract.md), [TWS-3 TWVL](../spec/twvl.md) |
| List | `qicsilver inspect` | volume or image | a `tar tv`-style listing | [qicsilver inspect](../qicsilver/inspect.md) |
| Archive | `qicsilver tar` | volume or image | tar file and damage report | [qicsilver tar](../qicsilver/tar.md) |

Two programs split the work. `tw` owns everything that touches the
Greaseweazle and the drive, plus the flux-to-image step. `qicsilver` reads
images and needs no hardware, no USB stack and no drive profiles. `cinnabar`
is a viewer for captures and images; use it to see where a tape is damaged
([cinnabar](../cinnabar/index.md)).

`tw inspect FILE` shows the header of any of the three formats
([tw inspect](../tw/inspect.md)).

## Next steps

- [Install the tools](install.md).
