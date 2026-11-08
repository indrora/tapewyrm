# Themes and settings

This page covers `cinnabar`'s colour themes, keyboard and mouse controls, view
settings, and how it decodes in the background.

## Themes

Four colour schemes ship:

| Theme | Look |
|---|---|
| `sketch` | Light grey lanes and blues. The default. |
| `paper` | Warmer, lower contrast. |
| `dark` | Dark lanes. |
| `contrast` | High contrast, for projectors and low vision. |

Pick one at start with `--theme`, or switch at run time with View > Theme:

```sh
cinnabar captures/NAME.twtz --theme dark
```

Themes colour the lanes only. The menus, toolbar and panels follow your
platform's own look. The theme you pick in the window is not saved; the next
run starts with `--theme`'s value.

## Keyboard

| Key | Action |
|---|---|
| Left / Right | Previous / next segment. |
| Page Up / Page Down | Previous / next track. |
| The platform's start-of-line / end-of-line keys | First / last segment. On Windows and Linux these are Home and End. |
| Cmd + / Cmd - (macOS), Ctrl + / Ctrl - (others) | Zoom in / out. |
| 0 | Fit the current segment. |
| Shift+0 | Fit the whole track. |
| W | Toggle wide lanes. |
| Esc | Stop: cancel the load in flight and pause decoding this track. |
| The platform's Open shortcut (Cmd+O / Ctrl+O) | Open a file. |
| The platform's Quit shortcut | Quit. |

Resume has no key, so a stray Esc never starts work. Use the toolbar button
or View > Resume decoding.

## Mouse

| Action | Effect |
|---|---|
| Wheel over the lanes | Zoom the time or byte axis. All lanes share the axis. |
| Drag over the lanes | Pan along the axis. Lanes do not scroll vertically. |
| Hover over a span or byte | The status bar names it. |
| Click a span or byte | The Details panel shows its fields and a hex dump. |
| Drag the bottom edge of a lane's header | Resize that lane. The cursor changes over the grip. |
| Drag the window on the track strip | Scrub through the track. |
| Click the track strip | Jump to that point. |

## Menus

| Menu | Items |
|---|---|
| File | Open..., Export segment bytes..., Export selected bytes..., Quit |
| View | Stop, Resume decoding, Zoom in, Zoom out, Fit segment, Fit track, Wide lanes, Lane height, Details, Theme |
| Go | Previous / next segment, first / last segment, previous / next track |
| Help | About cinnabar |

File > Open accepts `.twrf`, `.twti` and `.twtz` files. It reads the volume
table with `--volume-profile guess` and no
[overrides](../qicsilver/overrides.md), whatever the command line said.

## View settings

| Setting | Where | Effect |
|---|---|---|
| Wide lanes | View > Wide lanes, toolbar, W | Taller lanes, with room for a label and a sublabel in every row and three readable rows of flux. |
| Lane height | View > Lane height, toolbar **Height** | Scales every lane's height: 100%, 125%, 150%, 200% or 250%. |
| Lane size | Drag a lane header's bottom edge | Sets one lane's height. |
| Details panel | View > Details | Shows or hides the panel. You can also float it or dock it elsewhere. |

None of these is saved between runs.

## Background decoding

`cinnabar` never blocks the window on a whole-track decode.

**Opening.** A file opens on a background thread; a TWTZ decompresses there.
The status bar says what is loading.

**Flux tracks.** Opening a capture track reads its header, tokenizes the flux
stream and cuts it into segments at the drive's INDEX pulses. The track
appears at once: the Flux lane is already drawable, and every segment is a
"decoding" placeholder. The segments then decode in a pool of worker
processes, the segments nearest the view first. When you move the view, the
segments not yet started are reordered around the new position. The lanes
redraw a few times a second as results land, and the status bar's progress
bar counts them.

The pool is shared by every track and has one worker per CPU core, less one.

**Image tracks and volumes.** An image track is placed on a background
thread, and a volume track is decompressed there. Both appear when they are
ready.

**One track at a time.** Only one capture track decodes at a time. Switching
to another track or pass pauses the one you leave; the segments already
decoding finish, the rest wait. When you come back, decoding resumes where it
stopped. Picking another track or pass while one is loading cancels that load:
the latest request wins.

**Stop and Resume.** Stop (Esc) cancels the load in flight and pauses decoding
of the track on screen. The button then turns into Resume, which restarts
decoding from the segment in view outwards.

**Closing.** Closing the window stops all background work and waits for it to
end before the window goes.

`--dump` waits for the whole track to decode, then prints it.

## See also

- [cinnabar](index.md)
- [Lanes](lanes.md)
- [Passes and merged reads](passes.md)
