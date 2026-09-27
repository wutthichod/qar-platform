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
    "Decoded", "DecodeReport", "RangeFinding", "StartTime", "SuspectFinding",
    "decode_bytes", "decode_file",
]

DECODER_VERSION = "2.1.0"

# Mnemonics carrying recorded UTC, as (year, month, day, hour, minute,
# second). Every FAP family names these differently, so each is tried in
# turn. Recorded UTC is worth this trouble: it is the only timestamp that
# says when the aircraft actually flew.
_UTC_SCHEMES: tuple[tuple[str, ...], ...] = (
    ("YEAR", "MONTH", "DAY", "UTC_HOUR", "UTC_MIN", "UTC_SEC"),      # Airbus AGS
    ("aYEAR", "aMONTH", "aDAY", "aGMTH", "aGMTM", "aGMTS"),          # Boeing AGS
)


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
        for key in ("tail_number", "flight_number", "origin", "destination",
                    "recorded_at", "start_time", "start_time_source", "clock_agreement"):
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
    clock_rate: float = 1.0

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


# A recorded clock may read up to this far from the frame count and still
# agree: one second covers a clock that ticks just after a subframe boundary,
# and the odd mid-flight correction.
_CLOCK_TOLERANCE_S = 1
# Below this share of agreeing seconds the recorded clock is not believed.
_CLOCK_MIN_AGREEMENT = 0.9
# A recorded clock running further than this from the frame count (7 s an
# hour) is not a drifting clock but a mis-decoded one.
_CLOCK_MAX_RATE_ERROR = 2e-3


@dataclass
class StartTime:
    """Where row zero sits on the wall clock, and how that was decided.

    ``source`` is the point. A start time from the aircraft's clock and one
    from the container's offload stamp look identical in a timestamp column,
    and they can be days apart.
    """

    value: datetime | None
    source: str                      # "recorded_utc", "container:<key>" or "none"
    agreement: float | None = None   # share of seconds consistent with value
    max_drift_s: int | None = None
    seconds_checked: int = 0
    clock_rate: float = 1.0          # recorded seconds per frame-count second
    notes: list[str] = field(default_factory=list)


def _per_second(s: Series, n_seconds: int) -> tuple[np.ndarray, np.ndarray]:
    """A series' latest sample at or before each whole second."""
    index = np.floor(np.arange(n_seconds) * s.rate_hz).astype(np.int64)
    index = np.clip(index, 0, s.values.size - 1)
    return s.values[index], s.valid[index]


def _recorded_readings(series: dict[str, Series], scheme: tuple[str, ...],
                       n_seconds: int) -> tuple[np.ndarray, np.ndarray]:
    """Every second holding a valid recorded date and time, as (second of
    recording, recorded time in epoch seconds).

    The fields are recorded at different rates -- the date often once per
    frame, the time every second -- so each is carried to a per-second grid
    first. A second whose reading is impossible (a 37th of the month, a
    minute of 61) is dropped on its own rather than failing the clock.
    """
    fields = []
    ok = np.ones(n_seconds, dtype=bool)
    for key in scheme:
        values, valid = _per_second(series[key], n_seconds)
        fields.append(np.where(valid, np.nan_to_num(values), 0).astype(np.int64))
        ok &= valid
    year, month, day, hour, minute, second = fields
    # Recorders write the year as two digits about as often as four.
    year = np.where(year < 100, year + 2000, year)
    ok &= (year >= 1970) & (month >= 1) & (month <= 12) & (day >= 1) & (day <= 31)
    ok &= (hour < 24) & (minute < 60) & (second < 60)

    months = ((year - 1970) * 12 + (month - 1)).astype("datetime64[M]")
    days = months.astype("datetime64[D]") + (day - 1).astype("timedelta64[D]")
    ok &= days.astype("datetime64[M]") == months        # rejects 30 February
    epoch = (days.astype("datetime64[s]").astype(np.int64)
             + hour * 3600 + minute * 60 + second)
    return np.arange(n_seconds)[ok], epoch[ok]


def _fit_clock(t: np.ndarray, epoch: np.ndarray) -> tuple[float, float, np.ndarray]:
    """The recorded clock as a line against the frame count:
    ``epoch = start + rate * t``. Returns (start, rate, residuals).

    A line, not a constant offset. The recorder's frame clock and the
    aircraft's UTC run at very slightly different rates -- about a second an
    hour on both B777 samples -- so over a twelve-hour sector a constant
    offset fits neither end, and a healthy clock looks broken.

    Theil-Sen gives the first estimate, so a corrupt stretch of readings
    cannot drag it; least squares then refines it on the readings that
    agree.
    """
    base = int(epoch.min())
    x = t.astype(np.float64)
    y = (epoch - base).astype(np.float64)

    pick = np.unique(np.linspace(0, x.size - 1, min(x.size, 256)).astype(np.int64))
    i, j = np.triu_indices(pick.size, k=1)
    dx = x[pick][j] - x[pick][i]
    slopes = (y[pick][j] - y[pick][i])[dx > 0] / dx[dx > 0]
    rate = float(np.median(slopes)) if slopes.size else 1.0
    start = float(np.median(y - rate * x))
    residual = y - (start + rate * x)

    inliers = np.abs(residual) <= _CLOCK_TOLERANCE_S
    if inliers.sum() >= 2 and np.ptp(x[inliers]) > 0:
        rate, start = (float(v) for v in np.polyfit(x[inliers], y[inliers], 1))
        residual = y - (start + rate * x)
    return base + start, rate, residual


def _resolve_start_time(series: dict[str, Series], meta: dict[str, Any],
                        duration_s: float) -> StartTime:
    """Recorded UTC if it is present and consistent; otherwise the
    container's timestamp, labelled as such.

    The start comes from a fit through every recorded second, not from the
    first sample. Trusting one sample means a single corrupt reading shifts
    every timestamp in the file, and the recorded clock that would catch it
    sits unread in the same table.
    """
    n_seconds = int(round(duration_s))
    notes: list[str] = []

    for scheme in _UTC_SCHEMES:
        if not all(k in series and series[k].valid.any() for k in scheme):
            continue
        t, epoch = _recorded_readings(series, scheme, n_seconds)
        if t.size == 0:
            notes.append(f"recorded UTC ({', '.join(scheme)}) decoded, but no "
                         "second holds a valid date and time")
            continue
        start, rate, residual = _fit_clock(t, epoch)
        # A date recorded more slowly than the time of day rolls over a
        # moment after midnight: 00:00:00 can still carry yesterday's date
        # and read exactly a day early. That is the field's sampling, not a
        # clock fault, so fold it back rather than report a day of drift.
        lag = np.abs(np.abs(residual) - 86400) <= _CLOCK_TOLERANCE_S
        residual = np.where(lag, residual - np.sign(residual) * 86400, residual)
        drift = np.abs(residual)
        agreement = float((drift <= _CLOCK_TOLERANCE_S).mean())
        if agreement < _CLOCK_MIN_AGREEMENT:
            notes.append(f"recorded UTC ({', '.join(scheme)}) ignored: only "
                         f"{agreement:.1%} of seconds agree with a steady clock")
            continue
        if abs(rate - 1.0) > _CLOCK_MAX_RATE_ERROR:
            notes.append(f"recorded UTC ({', '.join(scheme)}) ignored: it runs at "
                         f"{rate:.5f}x the frame count, which no clock does")
            continue
        gain = (rate - 1.0) * n_seconds
        if abs(gain) > _CLOCK_TOLERANCE_S:
            notes.append(f"recorded clock runs {(rate - 1.0) * 3600:+.2f}s/h against "
                         f"the frame count ({gain:+.1f}s over the recording); "
                         "timestamps follow the recorded clock")
        if drift.max() > _CLOCK_TOLERANCE_S:
            notes.append(f"recorded clock strays up to {drift.max():.0f}s from a "
                         f"steady rate on {1 - agreement:.2%} of seconds")
        return StartTime(
            value=datetime.fromtimestamp(round(start), tz=timezone.utc),
            source="recorded_utc",
            agreement=agreement,
            max_drift_s=int(round(drift.max())),
            seconds_checked=int(t.size),
            clock_rate=rate,
            notes=notes,
        )

    for key in ("recorded_at", "shipped_at"):
        stamp = meta.get(key)
        if not stamp:
            continue
        try:
            value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        notes.append(f"start_time is the container's {key} ({stamp}), not the "
                     "aircraft clock; it can be days after the flight")
        return StartTime(value=value, source=f"container:{key}", notes=notes)

    notes.append("no start time: no usable recorded UTC and no container timestamp")
    return StartTime(value=None, source="none", notes=notes)

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

    start = _resolve_start_time(series, recording.metadata, fs.duration_s)
    report.notes.extend(start.notes)
    report.container_metadata["start_time_source"] = start.source
    if start.value is not None:
        report.container_metadata["start_time"] = start.value.isoformat()
    if start.agreement is not None:
        report.container_metadata["clock_agreement"] = f"{start.agreement:.6f}"
        report.container_metadata["clock_max_drift_s"] = str(start.max_drift_s)
        report.container_metadata["clock_rate"] = f"{start.clock_rate:.8f}"
    return Decoded(report=report, series=series, frames=fs,
                   start_time=start.value, clock_rate=start.clock_rate)


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
    # Scaled by the fitted clock rate, so the last row of a long sector sits
    # where the aircraft's clock says it does, not where the frame count does.
    offsets = (decoded.timebase(rate_hz) * decoded.clock_rate * 1e9).astype("timedelta64[ns]")
    return base + offsets
