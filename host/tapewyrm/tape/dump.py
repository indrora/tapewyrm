"""`tw dump`: whole tape tracks -> TWRF captures, gently.

Step one of ``tw dump -> tw convert -> tw extract``. One Logical Forward pass
per track, streamed straight to disk as a TWRF container (``track-NN.twrf``:
:mod:`tapewyrm.rawflux.container`). Its header records everything needed to
decode the flux later without guessing -- above all the bit rate, taken from
Report Drive Configuration (the rate the drive uses for Logical Forward with
this cartridge, QIC-117 Rev J (8)) -- plus the drive's raw status,
configuration, ROM, vendor ID and tape status bytes and the tw/firmware
commits that produced it.

Dumping only gets transitions off the tape; judging the data is ``tw
convert``'s job. What a dump does check after each pass is cheap and needs no
decoding (:func:`stream_health`): did the pass end at logical EOT, does the
stream match the firmware's END accounting, and how many segments did the drive
find? During Logical Forward the drive pulses INDEX once at the start of every
segment it finds (Rev J (10)), and nothing else: on the bench, 207 pulses for a
207-segment track, ~710 ms apart, no idle cue pulses. So INDEX counts segments,
and a long gap between pulses means segments the drive couldn't find. That
catches a tape that is shedding or a head that has lost the track. It can't see
garbage inside segments the drive still finds (the drive finds segments by
their erased gaps, not their data), so ``--check`` additionally decodes each
pass and applies a CRC rule.

The firmware ends each pass by itself when the tape stops at logical EOT (no
flux for 1 s), so nothing here depends on Stop Tape, which the bench drive
ignored during Logical Forward.

QIC-80 is serpentine: even tracks run toward physical EOT, odd tracks back
toward BOT, so track N+1 starts at the end where track N finished and a
sequential dump never rewinds the whole tape. But Logical Forward starts
reading wherever the tape happens to be (QIC-117 Rev J (10)), and where a pass
stops is already past the next track's first few segments: the first dump of
tape "jc" lost the first ~5 segments of every track after track 0 that way. So
each pass first winds to the end of the tape where its track starts
(:func:`wind_to_track_start`); in a sequential dump that is only the last few
feet. Old tape is fragile: after every pass the dump stops if anything
looks worse, rather than spending more passes on a tape that may be shedding.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from tapewyrm.codec import gwstream, mfm
from tapewyrm.link.device import LinkError
from tapewyrm.link.protocol import EndReason
from tapewyrm.progress import NULL_PROGRESS, Progress
from tapewyrm.qic117 import commands
from tapewyrm.qic117.drive import Qic117Drive
from tapewyrm.rawflux.container import read_header, write_preamble
from tapewyrm.types import (
    CaptureHeader,
    Direction,
    DriveConfig,
    DriveStatus,
    StopCond,
    TapeFormat,
    TapeStatus,
)

log = logging.getLogger(__name__)

# Stop rules (see check_pass). Each one means something on the tape or in the
# drive has degraded and a human should look before the next pass.
# --check only: fewer than this fraction of decoded sectors CRC-clean.
MIN_GOOD_FRACTION = 0.80
# Every track of one tape has the same segment count, so a pass whose INDEX
# count falls below this fraction of the best pass so far has lost segments.
MIN_INDEX_FRACTION = 0.90
# Estimated segments missed inside the pass (long INDEX gaps), as a fraction of
# the segments found.
MAX_MISSING_FRACTION = 0.05
# An INDEX gap this many times the pass's median gap hides missed segments.
LONG_GAP_FACTOR = 1.5
# A ready drive pulses INDEX every ~3 ms as a "cue" until Logical Forward takes
# over, so one can land at the very start of a capture (jc track 12: t = 0).
# Segments come ~700 ms apart and only after the leader, so pulses this early
# are cues, not segments. Same holdoff as the firmware's QIC_CUE_HOLDOFF_MS.
CUE_HOLDOFF_S = 0.050


def segment_pulses(index_ticks: list[int], sample_clock_hz: int) -> list[int]:
    """The INDEX pulses that mark segments: all but cue pulses at the start."""
    holdoff = CUE_HOLDOFF_S * sample_clock_hz
    return [t for t in index_ticks if t >= holdoff]


CAPTURE_SUFFIX = ".twrf"


class DumpStopped(Exception):
    """The dump stopped early on purpose; ``str()`` says why."""


@dataclass
class TrackResult:
    track: int
    path: str
    bytes: int
    seconds: float  # wall-clock for the pass
    end_reason: str  # EndReason name, or "none" if the stream had no END
    verified: bool  # our parse matched the firmware's END accounting
    tape_seconds: float  # flux time in the capture
    index_pulses: int  # segments the drive found (one INDEX per segment)
    missing_est: int  # segments estimated missed from long INDEX gaps
    status_after: int  # raw Report Drive Status after the pass
    error_after: int | None  # latched error code after the pass, if any
    # Only with --check (decoded); None otherwise.
    sectors: int | None = None
    good: int | None = None  # ID and data CRC both OK
    segments: int | None = None  # distinct segments seen in the sector IDs

    @property
    def good_fraction(self) -> float | None:
        if self.sectors is None or self.good is None:
            return None
        return self.good / self.sectors if self.sectors else 0.0


def missing_segments(index_ticks: list[int]) -> int:
    """Segments the drive probably skipped, from gaps between INDEX pulses.

    Segments pass the head at a steady rate, so INDEX pulses are evenly spaced
    (~710 ms on the 350 at 500 kbps). A gap of k median gaps hides k - 1
    segments. Only gaps over LONG_GAP_FACTOR x median count, so ordinary
    jitter (+-4% on the bench) never does. Segments missed before the first or
    after the last pulse can't be seen here; the INDEX-count rule covers those.
    """
    gaps = [b - a for a, b in zip(index_ticks, index_ticks[1:], strict=False)]
    if len(gaps) < 3:
        log.debug("only %d INDEX gaps (< 3); no median, assuming 0 missed", len(gaps))
        return 0
    median = sorted(gaps)[len(gaps) // 2]
    return sum(round(g / median) - 1 for g in gaps if g > LONG_GAP_FACTOR * median)


def check_pass(res: TrackResult, best_index: int, flux_ack: int = 0) -> str | None:
    """Why the dump should stop after this pass, or None to carry on.

    ``best_index`` is the highest INDEX count of the earlier passes in this
    dump (0 if this is the first).
    """
    if flux_ack != 0:
        log.debug("track %d: flux status %d != 0; stopping", res.track, flux_ack)
        return f"GW flux status {flux_ack} (overflow?)"
    if res.end_reason != EndReason.EOT.name:
        log.debug("track %d: end reason %s != EOT; stopping", res.track, res.end_reason)
        return f"pass ended with {res.end_reason}, not EOT"
    if not res.verified:
        log.debug("track %d: stream failed END verification; stopping", res.track)
        return "stream did not verify against END marker"
    if best_index and res.index_pulses < MIN_INDEX_FRACTION * best_index:
        log.debug(
            "track %d: %d INDEX < %.2f x best %d; stopping",
            res.track,
            res.index_pulses,
            MIN_INDEX_FRACTION,
            best_index,
        )
        return (
            f"drive found only {res.index_pulses} segments (best pass so far: "
            f"{best_index}); stopping to protect the tape"
        )
    if res.missing_est > MAX_MISSING_FRACTION * max(res.index_pulses, 1):
        log.debug(
            "track %d: %d missed > %.2f x %d found; stopping",
            res.track,
            res.missing_est,
            MAX_MISSING_FRACTION,
            res.index_pulses,
        )
        return (
            f"~{res.missing_est} segments missed inside the pass "
            f"({res.index_pulses} found); stopping to protect the tape"
        )
    fraction = res.good_fraction
    if fraction is not None and fraction < MIN_GOOD_FRACTION:
        log.debug(
            "track %d: good fraction %.3f < %.2f; stopping", res.track, fraction, MIN_GOOD_FRACTION
        )
        return (
            f"only {fraction:.0%} of sectors CRC-clean (< {MIN_GOOD_FRACTION:.0%}); "
            "stopping to protect the tape"
        )
    return None


def _report(drive: Qic117Drive, name: str, bits: int) -> int | None:
    """One raw report byte/word, or None if this drive doesn't implement it."""
    try:
        return drive.report(commands.TABLE[name], bits)
    except LinkError as exc:
        log.debug("%s unsupported (%s); clearing latched error, recording None", name, exc)
        drive.status()  # clear whatever the unsupported command latched
        return None


def drive_identity(drive: Qic117Drive) -> CaptureHeader:
    """A header template carrying the drive's reports, rate and provenance.

    Track/direction/time are filled in per pass by :func:`dump_tracks`.
    """
    from tapewyrm.buildinfo import host_build

    log.debug("reading drive identity reports")
    status = drive.status().raw
    config = _report(drive, "REPORT_DRIVE_CONFIGURATION", 8)
    tape = _report(drive, "REPORT_TAPE_STATUS", 8)
    if config is None:
        log.debug("no drive configuration; assuming 500 kbps")
    rate = DriveConfig.decode(config).rate_kbps if config is not None else 500
    fmt = TapeStatus.decode(tape).format if tape is not None else TapeFormat.UNKNOWN
    if fmt is TapeFormat.UNKNOWN and config is not None and DriveConfig.decode(config).qic80_mode:
        log.debug("tape format unknown but config 0x%02x says QIC-80 mode; assuming QIC-80", config)
        fmt = TapeFormat.QIC80  # Rev J Note 4
    info = drive.link.info
    if info is not None and info.proto_ver:
        log.debug("reading firmware build info")
    else:
        log.debug("no device INFO/proto_ver; firmware commit unknown")
    fw = drive.link.build_info() if info is not None and info.proto_ver else None
    return CaptureHeader(
        rate_kbps=rate,
        sample_clock_hz=(info.sample_clock_hz if info and info.sample_clock_hz else 72_000_000),
        track=0,
        direction=Direction.FORWARD,
        pass_id=1,
        utc="",
        tape_format=fmt,
        device_serial=info.serial if info else "",
        drive_status=status,
        drive_config=config,
        drive_rom=_report(drive, "REPORT_ROM_VERSION", 8),
        drive_vendor_id=_report(drive, "REPORT_VENDOR_ID", 16),
        tape_status=tape,
        tw_commit=host_build().commit,
        firmware_commit=fw.commit if fw else None,
        firmware_dirty=fw.dirty if fw else None,
    )


def wind_to_track_start(drive: Qic117Drive, track: int) -> DriveStatus:
    """Put the tape at the physical end where ``track`` begins.

    Even tracks run toward EOT, so they start at physical BOT: Physical Reverse.
    Odd tracks run toward BOT and start at physical EOT: Physical Forward. Both
    run at full speed, stop by themselves at the end of the tape, are a no-op
    if the tape is already there, and keep the drive not-Ready until the tape
    stops (Rev J (11)/(12)), so ``command`` returns the settled status. Seek
    Load Point would also reach BOT, but it re-references the tape (~28 s on
    the 350), so it isn't used here.

    Raises :class:`DumpStopped` unless the drive reports the expected end
    (``at_bot``/``at_eot``) with no error: reading from an unknown spot would
    silently lose segments again.
    """
    forward = Direction.for_track(track) is Direction.FORWARD
    cmd = commands.PHYSICAL_REVERSE if forward else commands.PHYSICAL_FORWARD
    log.debug(
        "track %d: winding with %s to physical %s", track, cmd.name, "BOT" if forward else "EOT"
    )
    st = drive.command(cmd)
    assert st is not None  # non-streaming motion always returns a status
    at_start = st.at_bot if forward else st.at_eot
    if st.error or not at_start:
        log.debug(
            "track %d: after wind error=%s at_start=%s (%s); refusing",
            track,
            st.error,
            at_start,
            st,
        )
        err = drive.last_error.code if (st.error and drive.last_error) else None
        raise DumpStopped(
            f"track {track}: {cmd.name} ended at {st} (error {err}), not at physical "
            f"{'BOT' if forward else 'EOT'}; refusing to read from an unknown position"
        )
    return st


def summarize(path: Path) -> tuple[CaptureHeader, gwstream.ParsedStream, list]:
    """Decode a TWRF capture with its own rate and clock (no assumptions)."""
    hdr, flux_at = read_header(path)
    log.debug("parsing flux stream of %s", path)
    ps = gwstream.parse(path.read_bytes()[flux_at:])
    log.debug("decoding sectors at %d kbps", hdr.rate_kbps)
    return hdr, ps, mfm.recover_sectors_from_flux(ps.intervals, ps.sample_clock_hz, hdr.rate_kbps)


def dump_tracks(
    drive: Qic117Drive,
    tracks: Iterable[int],
    out_dir: Path,
    *,
    progress: Progress = NULL_PROGRESS,
    check: bool = False,
) -> list[TrackResult]:
    """Capture each track in order; raise :class:`DumpStopped` on trouble.

    ``check`` also decodes every pass (~17 s each) and stops on a low
    CRC-clean fraction; see the module docstring.
    """
    from tapewyrm.qic117.status import error_name
    from tapewyrm.tape.geometry import coord_to_seg

    link = drive.link
    log.debug("creating output directory %s", out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = drive_identity(drive)
    log.info(
        f"drive: config 0x{template.drive_config or 0:02x} -> {template.rate_kbps} kbps, "
        f"tape {template.tape_format.name}; writing TWRF to {out_dir}"
    )
    tracks = list(tracks)
    results: list[TrackResult] = []
    with progress.task("dumping tracks", total=len(tracks), unit="tracks") as overall:
        for track in tracks:
            log.debug("track %d: checking drive is ready+referenced", track)
            st = drive.status()
            if not (st.ready and st.referenced) or st.error:
                log.debug(
                    "track %d: ready=%s referenced=%s error=%s; refusing",
                    track,
                    st.ready,
                    st.referenced,
                    st.error,
                )
                raise DumpStopped(
                    f"before track {track}: drive not ready+referenced ({st}); "
                    "Logical Forward would be refused (Rev J error 19)"
                )
            log.info(f"track {track:2d}: winding to its start...")
            t_wind = time.monotonic()
            wind_to_track_start(drive, track)
            log.info(f"track {track:2d}: wound to its start in {time.monotonic() - t_wind:.1f}s")
            log.debug("track %d: seeking head", track)
            drive.command(commands.SEEK_HEAD_TO_TRACK, arg=track)

            path = out_dir / f"track-{track:02d}{CAPTURE_SUFFIX}"
            hdr = replace(
                template,
                track=track,
                direction=Direction.for_track(track),
                utc=datetime.now(UTC).isoformat(timespec="seconds"),
                drive_status=st.raw,
            )
            log.info(f"track {track:2d}: capturing -> {path}")
            t0 = time.monotonic()
            cap = link.capture(
                commands.LOGICAL_FORWARD.code,
                StopCond(byte_budget=0),  # the tape ends the pass, not a budget
                rate=hdr.rate_kbps,
                tpt=track,
                direction=track & 1,
                pass_id=hdr.pass_id,
            )
            nbytes = 0
            # Logical Forward ends at EOT, so the length is unknown: count bytes.
            log.debug("track %d: streaming capture to %s", track, path)
            with path.open("wb") as f, progress.task(f"track {track:2d}", unit="bytes") as bar:
                write_preamble(f, hdr)
                for chunk in cap.chunks():
                    f.write(chunk)
                    nbytes += len(chunk)
                    bar.advance(len(chunk))
            log.debug("track %d: capture done, %d bytes; reading flux status", track, nbytes)
            flux_ack = link.flux_status()
            wall = time.monotonic() - t0

            log.debug("track %d: flux status %d; waiting for drive Ready", track, flux_ack)
            st = drive.wait_ready(30)
            err = drive.last_error.code if (st.error and drive.last_error) else None

            log.info(f"track {track:2d}: {nbytes / 1e6:.1f} MB in {wall:.0f}s; checking...")
            hdr_read, flux_at = read_header(path)
            ps = gwstream.parse(path.read_bytes()[flux_at:])
            segments_at = segment_pulses(ps.index_ticks, ps.sample_clock_hz)
            res = TrackResult(
                track=track,
                path=str(path),
                bytes=nbytes,
                seconds=round(wall, 1),
                end_reason=EndReason(ps.end.reason).name if ps.end else "none",
                verified=ps.verified,
                tape_seconds=round(ps.duration_s, 1),
                index_pulses=len(segments_at),
                missing_est=missing_segments(segments_at),
                status_after=st.raw,
                error_after=err,
            )
            summary = f"{res.index_pulses} segments by INDEX, {res.missing_est} missed in gaps"
            if check:
                log.info(f"track {track:2d}: decoding (--check)...")
                sectors = mfm.recover_sectors_from_flux(
                    ps.intervals, ps.sample_clock_hz, hdr_read.rate_kbps
                )
                res.sectors = len(sectors)
                res.good = sum(1 for s in sectors if s.id_crc_ok and s.data_crc_ok)
                res.segments = len({coord_to_seg(s.fsd, s.ftk, s.fsc) for s in sectors})
                summary += f"; {res.good}/{res.sectors} sectors good across {res.segments} segments"
            best_index = max((r.index_pulses for r in results), default=0)
            results.append(res)
            log.debug("track %d: appending result to %s", track, out_dir / "dump.jsonl")
            with (out_dir / "dump.jsonl").open("a") as f:
                f.write(json.dumps(asdict(res)) + "\n")
            log.info(
                f"track {track:2d}: END {res.end_reason}, {res.tape_seconds}s of tape, {summary}"
                + (f", error {err} {error_name(err)}" if err else "")
            )
            reason = check_pass(res, best_index, flux_ack)
            if reason is not None:
                raise DumpStopped(f"track {track}: {reason}")
            overall.advance()
    return results
