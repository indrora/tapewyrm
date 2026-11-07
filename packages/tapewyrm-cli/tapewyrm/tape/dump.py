"""`tw dump`: whole tape tracks -> TWRF captures, gently.

Step one of ``tw dump -> tw convert -> qicsilver extract``. One Logical Forward pass
per track, streamed straight to disk as a TWRF capture (``track-NN.twrf``:
:mod:`tapewyrm_archive.twrf`, TWS-1). Its header records everything needed to
decode the flux later without guessing -- above all the bit rate, taken from
Report Drive Configuration (the rate the drive uses for Logical Forward with
this cartridge, QIC-117 Rev J (8)) -- plus the drive's raw status,
configuration, ROM, vendor ID and tape status bytes and the tw/firmware
commits that produced it.

Dumping only gets transitions off the tape; judging the data is ``tw
convert``'s job. What a dump does check after each pass is cheap and needs no
decoding (:func:`check_pass`): did the pass end at logical EOT, does the
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

Old drives also read flakily, and many a failed pass is fixed by simply
reading the track again (the head settles differently, a USB hiccup does not
repeat, the drive finds the segment it skipped). ``--retries N`` re-reads a
track up to N more times (:func:`read_track`). A pass counts as failed when the
capture raised, when :func:`check_pass` would stop the dump on it, or when any
segment was missed inside it (``missing_est > 0``: stricter than the stop
rule's 5 %, because one re-read is cheap and one segment is 29 kB of someone's
files). The file kept is the last pass's unless an earlier one was better by
the same measures (:func:`pass_rank`); ``dump.jsonl`` accounts for every pass.
Only once the retries are spent do the stop rules judge the kept pass, exactly
as they judge a single pass without retries.

A dump that stopped, or a drive that went dark half-way, leaves a directory
with some tracks done. ``--no-clobber`` resumes it: a track whose latest
``dump.jsonl`` line is clean and whose file is whole is skipped
(:func:`previous_results`), and one that is not is read again with its
earlier capture seeded into the keep-best rule, so a new pass replaces the
file on disk only if it is better.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from tapewyrm_archive.progress import NULL_PROGRESS, Progress
from tapewyrm_archive.qic117 import DriveConfig, DriveStatus, TapeStatus
from tapewyrm_archive.twrf import read_header, write_preamble
from tapewyrm_archive.types import CaptureHeader, Direction, TapeFormat

from tapewyrm.codec import gwstream, mfm
from tapewyrm.link.device import LinkError
from tapewyrm.link.protocol import EndReason
from tapewyrm.qic117 import commands
from tapewyrm.qic117.drive import LINK_FATAL, DriveError, Qic117Drive
from tapewyrm.types import StopCond

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
# A re-read is captured beside the kept file as ``track-NN.twrf.retry`` and
# moved into place only if it wins (read_track). The name does not end in
# .twrf, so a run that dies mid-retry leaves nothing that `tw convert` or
# cinnabar would take for a capture; the next attempt overwrites it.
RETRY_SUFFIX = ".retry"
# The log of every pass, one JSON line each (docs/spec/twrf.md Appendix B),
# and what --no-clobber reads back (previous_results).
SIDECAR = "dump.jsonl"


def track_path(out_dir: Path, track: int) -> Path:
    """Where ``track``'s capture lives in ``out_dir``: ``track-NN.twrf``."""
    return out_dir / f"track-{track:02d}{CAPTURE_SUFFIX}"


class DumpStopped(Exception):
    """The dump stopped early on purpose; ``str()`` says why."""


@dataclass
class RejectedPass:
    """One pass of a track that ``--retries`` threw away, and why (:func:`read_track`)."""

    attempt: int
    reason: str


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
    # --retries (read_track): how many passes this track took, which of them
    # this line and the kept file describe, and why the others were deleted.
    # A track read clean first time is attempts=1, kept=1, rejected=[]. New
    # members go at the end: the sidecar is read by cinnabar and the
    # pipeline, which must find the older ones where they were.
    attempts: int = 1
    kept: int = 1
    rejected: list[RejectedPass] = field(default_factory=list)

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
    dump (0 if this is the first). The reasons name the measure alone; they
    are also what ``--retries`` records against a rejected pass, where
    "stopping" would be wrong (:func:`pass_failure`).
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
        return f"drive found only {res.index_pulses} segments (best pass so far: {best_index})"
    if res.missing_est > MAX_MISSING_FRACTION * max(res.index_pulses, 1):
        log.debug(
            "track %d: %d missed > %.2f x %d found; stopping",
            res.track,
            res.missing_est,
            MAX_MISSING_FRACTION,
            res.index_pulses,
        )
        return f"~{res.missing_est} segments missed inside the pass ({res.index_pulses} found)"
    fraction = res.good_fraction
    if fraction is not None and fraction < MIN_GOOD_FRACTION:
        log.debug(
            "track %d: good fraction %.3f < %.2f; stopping", res.track, fraction, MIN_GOOD_FRACTION
        )
        return f"only {fraction:.0%} of sectors CRC-clean (< {MIN_GOOD_FRACTION:.0%})"
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

    Track/direction/time are filled in per pass by :func:`_capture_pass`.
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


def tracks_for_tape(identity: CaptureHeader) -> list[int]:
    """Every track of the loaded cartridge, 0..N-1, from the drive's reports.

    This is what ``tw dump OUTDIR`` with no TRACKS captures. N comes from
    :func:`qiclib.geometry.track_count` for the format and width in Report Tape
    Status (cmd 33), so a QIC-80 tape on 0.250 in tape gives 0..27. Defaulting
    to the whole tape is safe because dumping only reads, and every pass is
    health-checked anyway. But a guessed count is not: if the drive can't say
    what format the tape is, we refuse and ask for explicit tracks rather than
    assume one (QIC-40 has 20 tracks, QIC-80 28, QIC-3010/3020 40).
    """
    from qiclib.geometry import track_count

    fmt = identity.tape_format
    if fmt is TapeFormat.UNKNOWN:
        log.debug("tape format unknown (tape status %r); refusing to guess", identity.tape_status)
        raise DumpStopped(
            "the drive did not report the tape's format, so the track count is unknown; "
            "name the tracks to dump, e.g. `tw dump OUTDIR 0-27`"
        )
    wide = identity.tape_status is not None and TapeStatus.decode(identity.tape_status).wide
    try:
        count = track_count(fmt, wide)
    except (KeyError, ValueError) as exc:
        log.debug("no track count for %s wide=%s: %r; refusing", fmt.name, wide, exc)
        raise DumpStopped(
            f"no track count known for {fmt.name} tape (wide={wide}); "
            "name the tracks to dump, e.g. `tw dump OUTDIR 0-27`"
        ) from exc
    log.info(f"no tracks named: dumping all {count} tracks of this {fmt.name} tape")
    return list(range(count))


# ---------------------------------------------------------------------------
# One pass of one track
# ---------------------------------------------------------------------------


def _capture_pass(
    drive: Qic117Drive,
    track: int,
    path: Path,
    attempt: int,
    *,
    template: CaptureHeader,
    progress: Progress = NULL_PROGRESS,
    check: bool = False,
) -> tuple[TrackResult, int]:
    """One Logical Forward pass over ``track`` into ``path``: (result, GW flux status).

    The whole dance for one pass, so that a re-read (:func:`read_track`)
    repeats it from the top: the ready+referenced check, the wind to the
    track's starting end, Seek Head to Track, the capture, the wait for Ready,
    and the decode-free measurements (plus the decode with ``check``).
    ``attempt`` goes into the TWRF header as ``pass_id`` (1 unless the track
    was re-read), so a capture says by itself that it is a re-read.

    Every drive command goes through the drive layer, which pulses with the
    profile's timing and resyncs a report that failed half-way (the TEAC
    FT-3010 goes dark until a power cycle if one is left half-clocked);
    nothing here, and nothing between attempts, touches the cable directly.

    Raises :class:`DumpStopped` when the drive is not fit to read, which is
    not retried: reading from an unknown position loses segments silently.
    A ``LinkError``/``DriveError`` from the capture is let through after the
    drive has had a chance to settle, so a retry starts from a stopped tape.
    """
    from qiclib.geometry import coord_to_seg

    from tapewyrm.qic117.status import error_name

    link = drive.link
    log.debug("track %d attempt %d: checking drive is ready+referenced", track, attempt)
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

    hdr = replace(
        template,
        track=track,
        direction=Direction.for_track(track),
        pass_id=attempt,
        utc=datetime.now(UTC).isoformat(timespec="seconds"),
        drive_status=st.raw,
    )
    log.info(
        f"track {track:2d}: capturing -> {path}" + (f" (attempt {attempt})" if attempt > 1 else "")
    )
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
    try:
        # `with cap` aborts the session if the stream dies mid-pass, and the
        # abort stops the tape (DESIGN.md §5.2); a stream that drained to its
        # END needs no abort and gets none.
        with cap, path.open("wb") as f, progress.task(f"track {track:2d}", unit="bytes") as bar:
            write_preamble(f, hdr)
            for chunk in cap.chunks():
                f.write(chunk)
                nbytes += len(chunk)
                bar.advance(len(chunk))
    except LinkError:
        # The tape is stopping but the drive is not Ready yet. Let it settle,
        # so a retry's ready check does not refuse a drive that is merely
        # busy; if even that fails, the capture's own error is the one worth
        # reporting, so it is the one raised.
        log.debug("track %d: capture raised; letting the drive settle before any retry", track)
        with suppress(LinkError, DriveError):
            drive.wait_ready(30)
        raise
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
    log.info(
        f"track {track:2d}: END {res.end_reason}, {res.tape_seconds}s of tape, {summary}"
        + (f", error {err} {error_name(err)}" if err else "")
    )
    return res, flux_ack


# ---------------------------------------------------------------------------
# Re-reading a failed track (--retries)
# ---------------------------------------------------------------------------


def pass_rank(res: TrackResult, flux_ack: int) -> tuple[bool, bool, bool, int, int, int]:
    """How good a pass is, for keeping the best of several: bigger is better.

    The measures are the ones :func:`pass_failure` judges by, most telling
    first: the pass ran to logical EOT (anything else cut the track short);
    the stream verified against the END accounting (else bytes went missing
    on the way); the GW flux status was clean (else the stream overflowed);
    then how many segments the drive found, which is the most direct count of
    how much of the track is in the file; then how few it skipped inside the
    pass; and with --check how many sectors decoded clean. Segments found
    outrank segments missed because a pass that found 207 and skipped 3
    inside holds more of the track than one that found 200 with no gap in
    it: the latter lost its segments at the ends of the pass, where no INDEX
    gap can show them (:func:`missing_segments`).
    """
    return (
        res.end_reason == EndReason.EOT.name,
        res.verified,
        flux_ack == 0,
        res.index_pulses,
        -res.missing_est,
        res.good or 0,
    )


def pass_failure(res: TrackResult, flux_ack: int, best_index: int) -> str | None:
    """Why this pass counts as failed for ``--retries``, or None if it is clean.

    Failed is what the capture path already knows how to see: anything
    :func:`check_pass` would stop the dump for (the pass did not end at EOT,
    the stream did not verify, the GW flux status reports an overflow, the
    drive found fewer segments than 90 % of the best track so far, too many
    were missed inside, or with --check too few sectors are CRC-clean), plus
    one stricter rule: a pass that skipped any segment inside
    (``missing_est > 0``) is worth another try, where the stop rule only
    reacts above 5 %. A capture that raised is failed too, but
    :func:`read_track` handles that before there is a result to judge.
    """
    reason = check_pass(res, best_index, flux_ack)
    if reason is not None:
        return reason
    if res.missing_est > 0:
        log.debug(
            "track %d: %d segments missed inside the pass; failed for --retries",
            res.track,
            res.missing_est,
        )
        return f"~{res.missing_est} segments missed inside the pass ({res.index_pulses} found)"
    return None


#: One pass into a file: ``capture_pass(dest, attempt) -> (result, flux status)``.
CapturePass = Callable[[Path, int], tuple[TrackResult, int]]


def read_track(
    capture_pass: CapturePass,
    track: int,
    path: Path,
    *,
    retries: int = 0,
    best_index: int = 0,
    existing: TrackResult | None = None,
) -> tuple[TrackResult, int]:
    """Read one track with up to ``retries`` re-reads: (kept result, its flux status).

    ``capture_pass(dest, attempt)`` makes one pass into ``dest`` and returns
    its measurements (:func:`_capture_pass` over a drive; the tests script
    it). The track is read again until the kept pass is clean
    (:func:`pass_failure`) or the attempts are spent; the stop rules are not
    applied here but by the caller, to what was kept.

    Which file stays at ``path``: the last pass's, unless an earlier one was
    better by :func:`pass_rank`, so a tie goes to the latest. In practice
    that is the clean pass, or the last of the failed ones. The two differ
    when a later pass is clean but found fewer segments than an earlier pass
    that skipped some inside; then the earlier file, which holds more of the
    track, is kept, and the loop goes on hoping for a pass that is both. A
    re-read is captured beside the kept file (``RETRY_SUFFIX``) and moved into
    place only if it wins, so the kept file is never overwritten by a worse
    one; the result's ``path`` is ``path`` whichever file it was captured
    into, and it carries ``attempts``, ``kept`` and, for every other pass,
    why that one was deleted.

    A pass that raised ``LinkError`` or ``DriveError`` has nothing to measure:
    it is rejected with the error as its reason. Its partial file is kept only
    while no pass has been measured at all, so a track that never read still
    leaves what it got (and a later raised pass replaces it: ties go to the
    latest). Errors that mean the board is gone (``LINK_FATAL``) are not
    retried. If every pass raised, the last error is raised again.

    ``existing`` is a pass already at ``path`` from an earlier run, with the
    measurements its ``dump.jsonl`` line recorded (``--no-clobber``). It
    competes like any pass, its flux status taken as clean (the sidecar does
    not record it, and an overflow shows as ``end_reason`` OVERFLOW anyway),
    and the new passes are numbered after its ``attempts``, so attempt
    numbers stay unique for the track and the new line accounts for every
    pass ever made on it in this directory.
    """
    if retries < 0:
        log.debug("retries=%d is negative; refusing", retries)
        raise ValueError(f"retries must be 0 or more, not {retries}")
    scratch = path.with_name(path.name + RETRY_SUFFIX)
    if scratch.exists():
        log.debug("track %d: deleting stale %s from an earlier run", track, scratch)
        scratch.unlink()
    kept: TrackResult | None = None
    kept_ack = 0
    kept_failure: str | None = None
    occupied = False  # something sits at `path`: the kept file, or a raised pass's partial
    rejected: list[RejectedPass] = []
    first = 1
    if existing is not None:
        kept = replace(existing, path=str(path))
        kept_failure = pass_failure(kept, kept_ack, best_index)
        occupied = True
        rejected = list(existing.rejected)
        first = existing.attempts + 1
        log.info(
            f"track {track:2d}: an earlier run left attempt {existing.kept} of "
            f"{existing.attempts} at {path} ({kept_failure or 'clean'}); a new pass "
            "replaces it only if better"
        )
    last_exc: Exception | None = None
    attempt = first - 1
    for attempt in range(first, first + retries + 1):
        dest = scratch if occupied else path
        if attempt > first:
            log.info(f"track {track:2d}: re-reading, attempt {attempt} of {first + retries}")
        try:
            res, flux_ack = capture_pass(dest, attempt)
        except LINK_FATAL:
            log.debug("track %d attempt %d: the board is gone; not retrying", track, attempt)
            raise
        except (LinkError, DriveError) as exc:
            why = f"capture raised {type(exc).__name__}: {exc}"
            log.warning(f"track {track:2d}: attempt {attempt} failed: {why}")
            rejected.append(RejectedPass(attempt, why))
            last_exc = exc
            if kept is None and dest.exists():
                log.debug(
                    "track %d attempt %d: nothing measured yet; its partial file stays as %s",
                    track,
                    attempt,
                    path,
                )
                if dest != path:
                    os.replace(dest, path)
                occupied = True
            else:
                log.debug("track %d attempt %d: deleting its partial %s", track, attempt, dest)
                dest.unlink(missing_ok=True)
            continue
        failure = pass_failure(res, flux_ack, best_index)
        if kept is not None and pass_rank(res, flux_ack) < pass_rank(kept, kept_ack):
            why = (f"{failure}; " if failure else "clean, but ") + (
                f"ranked below attempt {kept.kept} ({res.index_pulses} segments found, "
                f"{res.missing_est} missed, vs {kept.index_pulses} and {kept.missing_est})"
            )
            log.info(f"track {track:2d}: attempt {attempt} {why}; keeping attempt {kept.kept}")
            rejected.append(RejectedPass(attempt, why))
            dest.unlink()
        else:
            if kept is not None:
                # The kept pass was failed (a clean one ends the loop), and this
                # one ranks at least as high: it takes over, the old one goes.
                assert kept_failure is not None
                log.info(
                    f"track {track:2d}: attempt {attempt} replaces attempt {kept.kept} "
                    f"({kept_failure})"
                )
                rejected.append(RejectedPass(kept.kept, kept_failure))
            if dest != path:
                log.debug(
                    "track %d attempt %d: moving %s into place as %s", track, attempt, dest, path
                )
                os.replace(dest, path)
            kept = replace(res, path=str(path), kept=attempt)
            kept_ack, kept_failure = flux_ack, failure
            occupied = True
            if failure is not None:
                log.warning(f"track {track:2d}: attempt {attempt} failed: {failure}")
        if kept_failure is None:
            log.debug("track %d: attempt %d is clean; done", track, attempt)
            break
    if kept is None:
        assert last_exc is not None  # the loop ran at least once, and every pass raised
        log.warning(f"track {track:2d}: no pass could be measured in {attempt} attempt(s)")
        raise last_exc
    kept.attempts = attempt
    kept.rejected = sorted(rejected, key=lambda r: r.attempt)
    if attempt > 1:
        log.info(f"track {track:2d}: kept attempt {kept.kept} of {attempt}")
    return kept, kept_ack


def previous_results(out_dir: Path) -> dict[int, TrackResult]:
    """The last recorded pass of each track in ``out_dir``, read back from its sidecar.

    What ``--no-clobber`` consults. The latest line for a track is the one
    that counts: a directory dumped twice holds both runs' lines, and a
    re-read's line supersedes the earlier one. A line counts only if the
    track's file (:func:`track_path`) is there and holds at least the
    recorded flux bytes, so a capture cut short by a crash, or deleted by
    hand, is read again rather than trusted. Members this version does not
    know are ignored, lines from before ``--retries`` read as one pass, and
    a line that cannot be read is logged and ignored.
    """
    sidecar = out_dir / SIDECAR
    if not sidecar.is_file():
        log.debug("no %s in %s; nothing dumped before", SIDECAR, out_dir)
        return {}
    known = {f.name for f in fields(TrackResult)}
    found: dict[int, TrackResult] = {}
    for number, line in enumerate(sidecar.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            members = {k: v for k, v in entry.items() if k in known}
            members["rejected"] = [RejectedPass(**r) for r in members.get("rejected", [])]
            res = TrackResult(**members)
        except (ValueError, TypeError) as exc:
            log.warning(f"{sidecar}:{number}: unreadable line ignored ({exc})")
            continue
        found[res.track] = res
    for track, res in list(found.items()):
        path = track_path(out_dir, track)
        if not path.is_file() or path.stat().st_size < res.bytes:
            log.debug(
                "track %d: %s lists it but %s is missing or short; not trusting the line",
                track,
                SIDECAR,
                path,
            )
            del found[track]
    log.debug("%s: %d tracks dumped before with their files present", SIDECAR, len(found))
    return found


# ---------------------------------------------------------------------------
# The dump
# ---------------------------------------------------------------------------


def dump_tracks(
    drive: Qic117Drive,
    out_dir: Path,
    tracks: Iterable[int] | None = None,
    *,
    progress: Progress = NULL_PROGRESS,
    check: bool = False,
    retries: int = 0,
    no_clobber: bool = False,
) -> list[TrackResult]:
    """Capture each track in order; raise :class:`DumpStopped` on trouble.

    ``tracks`` None means every track of the cartridge: :func:`tracks_for_tape`
    counts them from the tape format and width the drive reports. ``check``
    also decodes every pass (~17 s each) and stops on a low CRC-clean
    fraction; see the module docstring. ``retries`` re-reads a track whose
    pass failed up to that many more times (:func:`read_track`) before the
    stop rules judge the pass that was kept. ``no_clobber`` resumes an earlier
    dump of ``out_dir``: a track whose last recorded pass is clean and whose
    file is whole is skipped (and returned as it was), and one that is not is
    read again with that pass seeded into keep-best (:func:`previous_results`).
    """
    log.debug("creating output directory %s", out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = drive_identity(drive)
    log.info(
        f"drive: config 0x{template.drive_config or 0:02x} -> {template.rate_kbps} kbps, "
        f"tape {template.tape_format.name}; writing TWRF to {out_dir}"
    )
    tracks = list(tracks) if tracks is not None else tracks_for_tape(template)
    previous = previous_results(out_dir) if no_clobber else {}
    results: list[TrackResult] = []
    with progress.task("dumping tracks", total=len(tracks), unit="tracks") as overall:
        for track in tracks:
            path = track_path(out_dir, track)
            # The best pass so far counts the earlier run's too, so a resumed
            # dump judges its new passes against the whole tape it knows.
            best_index = max((r.index_pulses for r in [*results, *previous.values()]), default=0)
            earlier = previous.get(track)
            if earlier is not None and pass_failure(earlier, 0, best_index) is None:
                log.info(
                    f"track {track:2d}: already dumped clean ({earlier.index_pulses} segments "
                    f"by INDEX, attempt {earlier.kept} of {earlier.attempts}); skipping"
                )
                results.append(earlier)
                overall.advance()
                continue
            res, flux_ack = read_track(
                partial(
                    _capture_pass, drive, track, template=template, progress=progress, check=check
                ),
                track,
                path,
                retries=retries,
                best_index=best_index,
                existing=earlier,
            )
            results.append(res)
            log.debug("track %d: appending result to %s", track, out_dir / SIDECAR)
            with (out_dir / SIDECAR).open("a") as f:
                f.write(json.dumps(asdict(res)) + "\n")
            reason = check_pass(res, best_index, flux_ack)
            if reason is not None:
                raise DumpStopped(f"track {track}: {reason}")
            still = pass_failure(res, flux_ack, best_index)
            if still is not None:
                # Under the stop rules but not clean (a few segments missed
                # inside): carry on with it, as a dump without retries would.
                log.warning(
                    f"track {track:2d}: carrying on with a pass that has {still}"
                    + ("; retries spent" if retries else "; --retries would re-read it")
                )
            overall.advance()
    return results
