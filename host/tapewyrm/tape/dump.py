"""Dump whole tape tracks to raw capture files, gently.

One Logical Forward pass per track, streamed straight to disk as the device
sends it (``track-NN.raw``: the verbatim Tapewyrm/GW stream, decode later with
``tapewyrm.codec.gwstream``). The firmware ends each pass by itself when the
tape stops at logical EOT (no flux for 1 s), so nothing here depends on Stop
Tape, which the bench drive ignored during Logical Forward.

QIC-80 is serpentine: even tracks run toward physical EOT, odd tracks back
toward BOT, so track N+1 starts where track N ended and a sequential dump never
rewinds. Old tape is fragile; after every pass we check the drive and decode
the capture, and stop the dump if anything looks worse rather than spending
more passes on a tape that may be shedding.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from tapewyrm.codec import gwstream, mfm
from tapewyrm.link.protocol import EndReason
from tapewyrm.qic117 import commands
from tapewyrm.qic117.drive import Qic117Drive
from tapewyrm.types import StopCond

# A track whose decodable sectors are less than this fraction CRC-clean stops
# the dump: something (head, tape, PLL) has degraded and we want a human first.
MIN_GOOD_FRACTION = 0.80


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
    sectors: int
    good: int  # ID and data CRC both OK
    segments: int  # distinct segments seen
    status_after: int  # raw Report Drive Status after the pass
    error_after: int | None  # latched error code after the pass, if any

    @property
    def good_fraction(self) -> float:
        return self.good / self.sectors if self.sectors else 0.0


def summarize(path: Path, rate_kbps: int) -> tuple[gwstream.ParsedStream, list]:
    ps = gwstream.parse(path.read_bytes())
    return ps, mfm.recover_sectors_from_flux(ps.intervals, ps.sample_clock_hz, rate_kbps)


def dump_tracks(
    drive: Qic117Drive,
    tracks: Iterable[int],
    out_dir: Path,
    *,
    rate_kbps: int = 500,
    log: Callable[[str], None] = print,
) -> list[TrackResult]:
    """Capture each track in order; raise :class:`DumpStopped` on trouble."""
    from tapewyrm.qic117.status import error_name

    link = drive.link
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[TrackResult] = []
    for track in tracks:
        st = drive.status()
        if not (st.ready and st.referenced) or st.error:
            raise DumpStopped(
                f"before track {track}: drive not ready+referenced ({st}); "
                "Logical Forward would be refused (Rev J error 19)"
            )
        drive.command(commands.SEEK_HEAD_TO_TRACK, arg=track)

        path = out_dir / f"track-{track:02d}.raw"
        log(f"track {track:2d}: capturing -> {path}")
        t0 = time.monotonic()
        cap = link.capture(
            commands.LOGICAL_FORWARD.code,
            StopCond(byte_budget=0),  # the tape ends the pass, not a budget
            rate=rate_kbps,
            tpt=track,
            direction=track & 1,
            pass_id=1,
        )
        nbytes = 0
        with path.open("wb") as f:
            for chunk in cap.chunks():
                f.write(chunk)
                nbytes += len(chunk)
        flux_ack = link.flux_status()
        wall = time.monotonic() - t0

        st = drive.wait_ready(30)
        err = drive.last_error.code if (st.error and drive.last_error) else None

        log(f"track {track:2d}: {nbytes / 1e6:.1f} MB in {wall:.0f}s; decoding...")
        ps, sectors = summarize(path, rate_kbps)
        from tapewyrm.tape.geometry import coord_to_seg

        res = TrackResult(
            track=track,
            path=str(path),
            bytes=nbytes,
            seconds=round(wall, 1),
            end_reason=EndReason(ps.end.reason).name if ps.end else "none",
            verified=ps.verified,
            tape_seconds=round(ps.duration_s, 1),
            sectors=len(sectors),
            good=sum(1 for s in sectors if s.id_crc_ok and s.data_crc_ok),
            segments=len({coord_to_seg(s.fsd, s.ftk, s.fsc) for s in sectors}),
            status_after=st.raw,
            error_after=err,
        )
        results.append(res)
        with (out_dir / "dump.jsonl").open("a") as f:
            f.write(json.dumps(asdict(res)) + "\n")
        log(
            f"track {track:2d}: END {res.end_reason}, {res.tape_seconds}s of tape, "
            f"{res.good}/{res.sectors} sectors good across {res.segments} segments"
            + (f", error {err} {error_name(err)}" if err else "")
        )

        if flux_ack != 0:
            raise DumpStopped(f"track {track}: GW flux status {flux_ack} (overflow?)")
        if res.end_reason != EndReason.EOT.name:
            raise DumpStopped(f"track {track}: pass ended with {res.end_reason}, not EOT")
        if not res.verified:
            raise DumpStopped(f"track {track}: stream did not verify against END marker")
        if res.good_fraction < MIN_GOOD_FRACTION:
            raise DumpStopped(
                f"track {track}: only {res.good_fraction:.0%} of sectors CRC-clean "
                f"(< {MIN_GOOD_FRACTION:.0%}); stopping to protect the tape"
            )
    return results
