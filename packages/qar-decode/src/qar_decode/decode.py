"""The pipeline: a file and a FAP in, decoded parameters and a report out.

    bytes -> container.unwrap -> frames.build -> parameters.decode

Every stage records what it found. A decode that goes wrong is almost
always wrong at one identifiable stage -- the container was not unwrapped,
sync did not lock, or the FAP does not match the recording -- and the report
is what tells them apart without a debugger.

Nothing here knows about S3, Iceberg or a scheduler. That is deliberate and
tests/test_import_boundary.py enforces it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from qar_decode import frames as frames_mod
from qar_decode import parameters as params_mod
from qar_decode.container import Recording, unwrap
from qar_decode.fap import Fap, load_fap
from qar_decode.frames import FRAME_SECONDS
from qar_decode.parameters import Series

__all__ = [
    "Decoded", "DecodeReport", "RangeFinding", "SuspectFinding",
    "decode_bytes", "decode_file",
]

# Bump on any change that alters decoded output, not only on releases: a
# stamped version is the only way to scope a re-decode, and two files
# claiming 1.0.0 that disagree cannot be told apart after the fact.
#   1.1.0  all-ones no-computed-data no longer applied to signed fields,
#          enumerated states, or fields narrower than 8 bits
# 2.0.0: a frame is four seconds, not one. Every duration, sample rate and
# timestamp before this version is wrong by a factor of four, so anything
# stamped 1.x must be re-decoded rather than compared against 2.x output.
DECODER_VERSION = "2.0.0"

# Mnemonics that carry recorded UTC, in the order the fields combine.
_UTC = ("YEAR", "MONTH", "DAY", "UTC_HOUR", "UTC_MIN", "UTC_SEC")


@dataclass
class RangeFinding:
    """A parameter whose decoded values leave the range the FAP declares.

    Not an error. It is the signal that the FAP revision and the recording
    revision have drifted apart, which is the single most common cause of a
    decode that runs clean and produces wrong numbers.
    """

    mnemonic: str
    unit: str | None
    declared: tuple[float, float]
    observed: tuple[float, float]
    fraction_outside: float


@dataclass
class SuspectFinding:
    """A continuous parameter that does not move like a physical quantity.

    Airspeed cannot alternate between zero and full scale four times a
    second. When it appears to, the bits being read are not airspeed --
    almost always because the FAP revision and the recording revision have
    drifted apart, and this parameter is one of the words that moved.

    The test is the fraction of consecutive samples that jump by more than a
    quarter of the parameter's own declared operating range. Real signals,
    sampled at their own rate, essentially never do this; mis-mapped ones do
    it constantly. It catches what a range check cannot, because a mis-mapped
    parameter usually still lands inside its declared range.
    """

    mnemonic: str
    unit: str | None
    rate_hz: float
    jump_fraction: float
    declared_span: float


@dataclass
class DecodeReport:
    source: str
    container: str
    container_metadata: dict[str, Any]
    fap: str
    decoder_version: str
    subframe_words: int
    sync_offset: int
    sync_confidence: float
    n_frames: int
    duration_s: float
    frame_integrity: float
    decoded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    out_of_range: list[RangeFinding] = field(default_factory=list)
    suspect: list[SuspectFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started_at: str | None = None
    elapsed_s: float = 0.0

    def summary(self) -> str:
        lines = [
            f"source            {self.source}",
            f"container         {self.container}",
            f"fap               {self.fap}",
            f"decoder           {self.decoder_version}",
            f"subframe          {self.subframe_words} words, sync at word {self.sync_offset}",
            f"sync confidence   {self.sync_confidence:.6f}",
            f"frames            {self.n_frames} x {FRAME_SECONDS:g}s  "
            f"({int(self.duration_s) // 3600}h "
            f"{int(self.duration_s) % 3600 // 60}m {int(self.duration_s) % 60}s)",
            f"frame integrity   {self.frame_integrity * 100:.4f}%",
            f"parameters        {len(self.decoded)} decoded, {len(self.failed)} failed",
        ]
        for key in ("tail_number", "flight_number", "origin", "destination", "recorded_at"):
            if self.container_metadata.get(key):
                lines.append(f"{key:<18}{self.container_metadata[key]}")
        if self.out_of_range:
            lines.append(f"out of declared range  {len(self.out_of_range)}:")
            for f in self.out_of_range[:10]:
                lines.append(
                    f"  {f.mnemonic:<20} declared [{f.declared[0]:g}, {f.declared[1]:g}] "
                    f"observed [{f.observed[0]:.3g}, {f.observed[1]:.3g}] "
                    f"({f.fraction_outside * 100:.1f}% outside)"
                )
        if self.suspect:
            lines.append(f"implausibly discontinuous  {len(self.suspect)}:")
            for f in self.suspect[:10]:
                lines.append(
                    f"  {f.mnemonic:<20} {f.jump_fraction * 100:5.1f}% of consecutive "
                    f"samples jump >25% of range at {f.rate_hz:g} Hz"
                )
        for note in self.notes:
            lines.append(f"note              {note}")
        lines.append(f"elapsed           {self.elapsed_s:.1f}s")
        return "\n".join(lines)


@dataclass
class Decoded:
    report: DecodeReport
    series: dict[str, Series]
    frames: frames_mod.FrameSet
    start_time: datetime | None = None

    def by_rate(self) -> dict[float, list[str]]:
        """Group parameters by native rate in hertz.

        Silver stores one table per rate rather than one wide table at the
        maximum. Upsampling a 1 Hz parameter to sit beside an 8 Hz one
        multiplies its storage eightfold and adds no information.
        """
        out: dict[float, list[str]] = {}
        for name, s in sorted(self.series.items()):
            if s.samples_per_frame:
                out.setdefault(s.rate_hz, []).append(name)
        return dict(sorted(out.items()))

    def timebase(self, rate_hz: float) -> np.ndarray:
        """Seconds from the start of the recording, for one rate.

        The sample count comes from frames, not seconds: a frame holds
        ``rate_hz * FRAME_SECONDS`` samples of a parameter at this rate.
        """
        per_frame = int(round(rate_hz * FRAME_SECONDS))
        n = self.report.n_frames * per_frame
        return np.arange(n, dtype=np.float64) / rate_hz


def _resolve_start_time(series: dict[str, Series], meta: dict[str, Any]) -> datetime | None:
    """Recorded UTC if the FAP carried it, otherwise the container's claim.

    Recorded time is preferred: the container timestamp is when the file was
    offloaded, which on a long-haul rotation can be days after the flight.
    """
    have = {k: series[k] for k in _UTC if k in series and series[k].valid.any()}
    if len(have) == len(_UTC):
        try:
            first = {}
            for key, s in have.items():
                good = s.values[s.valid]
                first[key] = int(round(float(good[0])))
            year = first["YEAR"]
            year += 2000 if year < 100 else 0
            return datetime(
                year, first["MONTH"], first["DAY"],
                first["UTC_HOUR"], first["UTC_MIN"], first["UTC_SEC"],
                tzinfo=timezone.utc,
            )
        except (ValueError, KeyError, IndexError):
            pass

    stamp = meta.get("recorded_at") or meta.get("shipped_at")
    if stamp:
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _range_check(name: str, s: Series, param) -> RangeFinding | None:
    if param.min_op is None or param.max_op is None or param.is_discrete:
        return None
    if param.min_op == param.max_op:
        return None
    good = s.values[s.valid]
    if good.size == 0:
        return None
    # A little slack: FAP operating ranges are nominal, and a real recording
    # touches its limits without being wrong.
    span = param.max_op - param.min_op
    lo, hi = param.min_op - 0.02 * span, param.max_op + 0.02 * span
    outside = float(((good < lo) | (good > hi)).mean())
    if outside <= 0.001:
        return None
    return RangeFinding(
        mnemonic=name,
        unit=s.unit,
        declared=(param.min_op, param.max_op),
        observed=(float(good.min()), float(good.max())),
        fraction_outside=outside,
    )


def _suspect_check(name: str, s: Series, param) -> SuspectFinding | None:
    if param.min_op is None or param.max_op is None or param.is_discrete:
        return None
    span = param.max_op - param.min_op
    if span <= 0 or s.samples_per_frame < 2:
        return None
    good = np.isfinite(s.values)
    if good.sum() < 100:
        return None
    values = np.where(good, s.values, np.nan)
    jumps = np.abs(np.diff(values))
    jumps = jumps[np.isfinite(jumps)]
    if jumps.size < 100:
        return None
    fraction = float((jumps > 0.25 * span).mean())
    if fraction < 0.05:
        return None
    return SuspectFinding(
        mnemonic=name, unit=s.unit, rate_hz=s.rate_hz,
        jump_fraction=fraction, declared_span=span,
    )


def decode_bytes(
    data: bytes,
    fap: Fap,
    source: str = "<bytes>",
    only: set[str] | None = None,
) -> Decoded:
    started = time.time()
    recording: Recording = unwrap(data)
    fs = frames_mod.build(recording.words, fap.frame)

    wanted = only or set(fap.parameters)
    series: dict[str, Series] = {}
    failed: dict[str, str] = {}
    findings: list[RangeFinding] = []
    suspects: list[SuspectFinding] = []

    for name in sorted(wanted):
        param = fap.parameters.get(name)
        if param is None:
            failed[name] = "not in FAP"
            continue
        try:
            s = params_mod.decode(fs, param)
        except Exception as exc:                      # noqa: BLE001
            failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        if s.samples_per_frame == 0:
            failed[name] = "no usable locations"
            continue
        series[name] = s
        finding = _range_check(name, s, param)
        if finding is not None:
            findings.append(finding)
        suspect = _suspect_check(name, s, param)
        if suspect is not None:
            suspects.append(suspect)

    findings.sort(key=lambda f: -f.fraction_outside)
    suspects.sort(key=lambda f: -f.jump_fraction)

    notes = list(recording.notes)
    if fs.sync.subframe_size != fap.frame.subframe_words:
        notes.append(
            f"file syncs at {fs.sync.subframe_size} words but the FAP declares "
            f"{fap.frame.subframe_words}; the file was believed"
        )

    report = DecodeReport(
        source=source,
        container=recording.container,
        container_metadata=recording.metadata,
        fap=fap.name,
        decoder_version=DECODER_VERSION,
        subframe_words=fs.subframe_words,
        sync_offset=fs.sync.offset,
        sync_confidence=fs.sync.confidence,
        n_frames=fs.n_frames,
        duration_s=fs.duration_s,
        frame_integrity=fs.integrity,
        decoded=sorted(series),
        failed=failed,
        out_of_range=findings,
        suspect=suspects,
        notes=notes,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        elapsed_s=round(time.time() - started, 3),
    )

    start = _resolve_start_time(series, recording.metadata)
    if start is not None:
        report.container_metadata.setdefault("start_time", start.isoformat())
    return Decoded(report=report, series=series, frames=fs, start_time=start)


def decode_file(
    path: str | Path,
    fap: str | Path | Fap,
    only: set[str] | None = None,
) -> Decoded:
    path = Path(path)
    if not isinstance(fap, Fap):
        fap = load_fap(fap, only=only)
    return decode_bytes(path.read_bytes(), fap, source=str(path), only=only)


def timestamps(decoded: Decoded, rate_hz: float) -> np.ndarray | None:
    """Absolute UTC per sample for one rate, when a start time is known."""
    if decoded.start_time is None:
        return None
    base = np.datetime64(decoded.start_time.replace(tzinfo=None), "ns")
    offsets = (decoded.timebase(rate_hz) * 1e9).astype("timedelta64[ns]")
    return base + offsets
