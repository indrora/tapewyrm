"""TapeTransport — logical motion + capture orchestration (DESIGN.md §6A.4).

Turns a track number into a ``RawFluxCapture``. Serpentine is handled by the
drive (Logical Forward presents data in logical order regardless of physical
direction), so this layer **never reverses flux** — it only records ``direction``
in the capture header for the codec.

Key hazards honored here:

* Do **not** wait-ready/status between issuing Logical Forward and arming capture
  — a status report can swallow segment 0 (DESIGN.md §2.1/§6A.4). We therefore
  drive capture through ``drive.link.capture(LOGICAL_FORWARD.code, stop)`` and
  never call ``drive.command(LOGICAL_FORWARD)``.
* Broken-tape -> abort the capture and raise (DESIGN.md §6A.4).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from tapewyrm_archive.twrf import RawFluxCapture
from tapewyrm_archive.types import CaptureHeader, TapeFormat

from tapewyrm.link.device import LinkError
from tapewyrm.qic117 import commands
from tapewyrm.qic117.drive import Qic117Drive
from tapewyrm.qic117.status import classify_error
from tapewyrm.tape.geometry import Geometry
from tapewyrm.types import DriveConfig, StopCond, TapeStatus

log = logging.getLogger(__name__)


class TapeError(Exception):
    """A tape-transport failure (positioning, capture, or a fatal drive error)."""


def _utc_now() -> str:
    """ISO-8601 UTC timestamp. Hardware-facing transport, so a real clock is fine
    (DESIGN.md §6A.4 — only the *pure codec* is forbidden a clock)."""
    return datetime.now(UTC).isoformat()


class TapeTransport:
    """Logical motion + geometry; turns a track into a RawFluxCapture (§6A.4)."""

    def __init__(self, drive: Qic117Drive) -> None:
        self.drive = drive
        self._cfg: DriveConfig | None = None
        self._tape: TapeStatus | None = None
        self._geom: Geometry | None = None

    @property
    def geometry(self) -> Geometry:
        if self._geom is None:
            log.debug("geometry requested before identify(); refusing")
            raise TapeError("geometry unknown — call identify() first")
        return self._geom

    @property
    def config(self) -> DriveConfig:
        if self._cfg is None:
            log.debug("drive config requested before identify(); refusing")
            raise TapeError("drive config unknown — call identify() first")
        return self._cfg

    # --- identification ---

    def identify(self) -> tuple[DriveConfig, TapeStatus, Geometry]:
        """Wake, read config + tape status, derive geometry (DESIGN.md §6A.4).

        Geometry uses the reported segments-per-track when the drive supports it
        (Report Format Segments, cmd 37, CCS-2); otherwise it falls back to the
        QIC-117 fixed spt override inside ``Geometry.for_format`` (DESIGN.md §7.3).
        """
        log.debug("identify: waking drive")
        self.drive.wake()
        log.debug("identify: reading drive configuration")
        cfg = self.drive.config()
        unsupported = False
        log.debug("identify: reading tape status (cmd 33)")
        try:
            tape = self.drive.tape_status()  # cmd 33 (CCS-1)
        except LinkError as exc:
            # Pre-CCS-1 drives (e.g. the bench Colorado Jumbo 350) never ACK
            # Report Tape Status. QIC-117 Rev J Note 4: if Report Drive
            # Configuration says QIC-80, the host may assume a QIC-80 tape.
            unsupported = True
            fmt = TapeFormat.QIC80 if cfg.qic80_mode else TapeFormat.QIC40
            log.debug(
                "tape status unsupported (%s); assuming %s from qic80_mode=%s",
                exc,
                fmt.name,
                cfg.qic80_mode,
            )
            tape = TapeStatus(format=fmt, tape_type=0, wide=False, raw=0)

        spt: int | None = None
        log.debug("identify: reading format segments (cmd 37)")
        try:
            reported = self.drive.format_segments()  # cmd 37 (CCS-2)
            spt = reported if reported > 0 else None
            if spt is None:
                log.debug("drive reported %d segments/track; using fixed fallback", reported)
        except LinkError as exc:
            # Basic drive lacking 36/37: fall back to fixed geometry (§2.1, §7.3).
            log.debug("format segments unsupported (%s); using fixed fallback geometry", exc)
            unsupported = True
            spt = None

        if unsupported:
            log.debug("identify: an unsupported command may have latched; reading status to clear")
            # An unsupported command may latch "undefined command"; read status
            # (which reads+clears the error) so later commands aren't rejected.
            self.drive.status()

        geom = Geometry.for_format(tape.format, segments_per_track=spt, wide=tape.wide)
        self._cfg, self._tape, self._geom = cfg, tape, geom
        return cfg, tape, geom

    # --- positioning ---

    def load_point(self) -> None:
        """Seek load point (cmd 14)."""
        log.debug("seeking load point")
        self.drive.command(commands.SEEK_LOAD_POINT)

    def seek_track(self, t: int) -> None:
        """Seek head to track (cmd 13), operand as N+2 pulses (DESIGN.md §2.1)."""
        log.debug("seeking head to track %d", t)
        self.drive.command(commands.SEEK_HEAD_TO_TRACK, arg=t)

    # --- capture ---

    def capture_pass(self, track: int, pass_id: int) -> RawFluxCapture:
        """Capture one Logical-Forward pass over ``track`` (DESIGN.md §6A.4).

        Builds the CaptureHeader (rate/clock/track/direction/pass_id/utc), derives
        the byte-budget stop condition from geometry, then opens the capture
        session and drains it into a RawFluxCapture. Note: capture is armed via
        ``link.capture`` so we never wait-ready/status after Logical Forward.
        """
        geom = self.geometry
        cfg = self.config

        self.seek_track(track)

        clock = self._sample_clock()
        hdr = CaptureHeader(
            rate_kbps=cfg.rate_kbps,
            sample_clock_hz=clock,
            track=track,
            direction=geom.direction(track),
            pass_id=pass_id,
            utc=_utc_now(),
            tape_format=self._tape.format if self._tape else CaptureHeader.tape_format,
            segments_per_track=geom.segments_per_track,
            tracks=geom.tracks,
            device_serial=self._device_serial(),
        )
        stop = StopCond(byte_budget=geom.byte_budget(cfg.rate_kbps))

        log.debug(
            "capturing track %d pass %d at %d kbps, %s, byte budget %d",
            track,
            pass_id,
            cfg.rate_kbps,
            hdr.direction.name,
            stop.byte_budget,
        )
        with self.drive.link.capture(commands.LOGICAL_FORWARD.code, stop) as cap:
            return RawFluxCapture.from_stream(hdr, cap.chunks())

    def walk_all(self, passes: int = 1) -> Iterator[RawFluxCapture]:
        """Serpentine walk over all tracks, ``passes`` per track (DESIGN.md §6A.4).

        Serpentine is handled by the drive; this only records ``direction`` in the
        header. Before each pass the broken-tape guard runs: a fatal drive error
        aborts and raises.
        """
        geom = self.geometry
        for t in range(geom.tracks):
            for p in range(passes):
                self._guard_hazards()
                yield self.capture_pass(t, p)

    # --- hazards ---

    def _guard_hazards(self) -> None:
        """Broken-tape guard: read status, abort+raise on a fatal error (§6A.4)."""
        st = self.drive.status()
        if st.error:
            ec = self.drive.last_error
            code = ec.code if ec is not None else 0
            if classify_error(code):
                # Stop the tape before raising (DESIGN.md §5.2 — stop, always).
                log.debug("fatal drive error %d; sending Stop Tape before aborting", code)
                try:
                    self.drive.command(commands.STOP_TAPE)
                except Exception as exc:
                    log.debug("Stop Tape failed (%s); aborting anyway", exc)
                raise TapeError(
                    f"fatal drive error {code} (broken tape?) — aborting sweep (DESIGN.md §6A.4)"
                )

    # --- helpers ---

    def _sample_clock(self) -> int:
        """Sample clock used to stamp the capture header.

        TODO(bench), DESIGN.md §13.6 item 1: the real sample-clock tick rate comes
        from the device INFO / flux engine; until the GW flux encoding is read,
        stamp the AT32F403 nominal 72 MHz placeholder.
        """
        info = self.drive.link.info
        # No standard field carries the flux sample clock yet; use a nominal value.
        if info is not None and info.sram_bytes:  # info present -> link is open
            pass
        return 72_000_000

    def _device_serial(self) -> str:
        info = self.drive.link.info
        if info is None:
            log.debug("no device INFO; recording empty device serial")
        return info.serial if info is not None else ""
