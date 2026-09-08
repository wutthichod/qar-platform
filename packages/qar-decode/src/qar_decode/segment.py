"""Flight segmentation -- the Bronze+ layer.

A recorder file boundary does not respect flight boundaries. One file can
hold the tail of one flight and the start of another; a long sector can span
two files. This module decides where flights begin and end.

The signal used, in order of preference:

  1. an air/ground discrete, when the FAP has one and it actually toggles --
     this is what the aircraft itself believes, and nothing derived beats it
  2. radio altitude, which is unambiguous near the ground and is the reason
     a derived answer can be trusted at all
  3. ground speed, which is zero at the gate and needs no threshold
     tuning per type
  4. airspeed, as a last resort -- it is the signal most often mis-mapped
     in a FAP whose revision has drifted from the recording

Every path is debounced. A weight-on-wheels discrete chatters through the
bounce and the derotation, and an undebounced reading turns one landing into
four, which then becomes four flights in the registry. The same is true of a
radio altimeter unlocking briefly in the flare.

Segments are returned even when they are partial -- a file that opens
mid-cruise has no takeoff, and saying so is more useful than discarding the
sector.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from qar_decode.frames import FRAME_SECONDS

__all__ = ["FlightSegment", "SegmentInputs", "segment", "segment_decoded"]

# Mnemonics that carry air/ground, airspeed and altitude, per FAP family.
# Ordered by preference; the first that is present and usable wins.
GROUND_DISCRETES = ("AC_GROUND", "ANY_GEAR_GND", "LH_GEAR_GND", "Ground_Phase",
                    "aAIRGND1", "aAIRGND2", "WSTAT108G")
RADIO_ALTITUDES = ("RALT1", "RALT2", "RALT_01", "aRALTC", "aRALTL", "aRALTR")
GROUND_SPEEDS = ("GS", "GS_FO", "aGS2", "aGS4", "aGS3")
AIRSPEEDS = ("CAS_CAP", "IAS", "CAS_ADR2", "aCAS1", "aCAS2")
ALTITUDES = ("ALT_STD", "aALTSTD1", "aALTSTD2")

# A jet is flying above this speed and stopped below it.
TAKEOFF_KT = 80.0
# Radio altitude above which the aircraft is certainly airborne.
AIRBORNE_FT = 20.0
# Ground speed above which the aircraft is rolling for takeoff, not taxiing.
ROLLING_KT = 60.0
# Debounce, in seconds. Shorter than a touch-and-go, longer than a bounce.
DEBOUNCE_S = 15
# A segment shorter than this is not a flight.
MIN_FLIGHT_S = 120


@dataclass(frozen=True)
class FlightSegment:
    """One flight's extent within a recording, indexed in **seconds**.

    Seconds, not frames. A frame is four seconds, so frame resolution puts a
    four-second error bar on every rotation and touchdown -- and an index
    labelled "frame" that is really a second is how that error gets into
    somebody's takeoff-performance statistics.

    The ``*_frame`` properties convert back for comparison against a vendor
    tool, which numbers frames rather than seconds.
    """

    flight_id: str
    tail_number: str | None
    first_second: int
    last_second: int
    takeoff_second: int | None = None
    touchdown_second: int | None = None

    @property
    def duration_s(self) -> int:
        return self.last_second - self.first_second + 1

    @property
    def airborne_s(self) -> int | None:
        if self.takeoff_second is None or self.touchdown_second is None:
            return None
        return self.touchdown_second - self.takeoff_second

    @property
    def complete(self) -> bool:
        return self.takeoff_second is not None and self.touchdown_second is not None

    @property
    def takeoff_frame(self) -> int | None:
        """Vendor frame number, for cross-checking against an FDM tool."""
        if self.takeoff_second is None:
            return None
        return int(self.takeoff_second // FRAME_SECONDS)

    @property
    def touchdown_frame(self) -> int | None:
        if self.touchdown_second is None:
            return None
        return int(self.touchdown_second // FRAME_SECONDS)


@dataclass
class SegmentInputs:
    """Per-second signals, all at 1 Hz, all optional but not all at once."""

    on_ground: np.ndarray | None = None      # bool
    radio_altitude_ft: np.ndarray | None = None
    ground_speed_kt: np.ndarray | None = None
    airspeed_kt: np.ndarray | None = None
    altitude_ft: np.ndarray | None = None
    source: str = ""


def _to_1hz(values: np.ndarray, samples_per_frame: int, n_frames: int) -> np.ndarray:
    """Resample a parameter to exactly one value per second.

    A frame is four seconds, so a parameter with four samples per frame is
    already 1 Hz and one with thirty-two is 8 Hz. Anything slower than 1 Hz
    -- and a great many FAP entries appear once per frame, which is 0.25 Hz
    -- is held across the seconds it does not cover.

    Faster than 1 Hz collapses by maximum, not mean: for the signals used
    here, a threshold crossing or a discrete going true, one sample of
    evidence within the second is the thing worth keeping.
    """
    n_seconds = int(n_frames * FRAME_SECONDS)
    per_second = samples_per_frame / FRAME_SECONDS

    if per_second >= 1:
        step = int(per_second)
        usable = n_seconds * step
        grid = values[:usable].reshape(n_seconds, step)
        with np.errstate(invalid="ignore"):
            return np.nanmax(grid, axis=1)

    # Slower than 1 Hz: hold each sample over the seconds it spans.
    span = int(round(1 / per_second))
    return np.repeat(values[: n_frames * samples_per_frame], span)[:n_seconds]


def _debounce(flags: np.ndarray, hold: int = DEBOUNCE_S) -> np.ndarray:
    """Remove interior runs shorter than ``hold`` samples.

    Repeatedly flips the shortest offending run and re-measures, because
    flipping one run merges its neighbours and can leave the result still
    too short to be real. A single pass looks like it works and leaves
    exactly the chatter it was written to remove.

    Runs at either end of the recording are left alone: a short run at a
    file boundary is a truncated segment, which is information, not noise.
    """
    out = np.asarray(flags, dtype=bool).copy()
    if out.size == 0:
        return out

    while True:
        starts = np.flatnonzero(np.r_[True, out[1:] != out[:-1]])
        lengths = np.diff(np.r_[starts, out.size])
        candidates = np.flatnonzero(lengths < hold)
        candidates = candidates[(candidates > 0) & (candidates < lengths.size - 1)]
        if candidates.size == 0:
            return out
        worst = candidates[np.argmin(lengths[candidates])]
        begin = starts[worst]
        out[begin : begin + lengths[worst]] = not out[begin]


def inputs_from_decoded(decoded) -> SegmentInputs:
    """Pick the best available signals out of a decode."""
    n = decoded.report.n_frames
    series = decoded.series

    def collapse(name: str) -> np.ndarray | None:
        s = series.get(name)
        if s is None or not s.valid.any():
            return None
        return _to_1hz(s.values, s.samples_per_frame, n)

    for name in GROUND_DISCRETES:
        s = series.get(name)
        if s is None or not s.valid.any():
            continue
        flags = _to_1hz(np.nan_to_num(s.values, nan=0.0), s.samples_per_frame, n) > 0.5
        # A discrete that never changes carries no information: some FAPs
        # map a bit the recorder never populated, and a constant "on ground"
        # would swallow the whole flight.
        if 0.01 < flags.mean() < 0.99:
            return SegmentInputs(on_ground=flags, source=f"discrete:{name}")

    def first(names) -> np.ndarray | None:
        for name in names:
            got = collapse(name)
            if got is not None and np.isfinite(got).any():
                return got
        return None

    radio = first(RADIO_ALTITUDES)
    ground = first(GROUND_SPEEDS)
    speed = first(AIRSPEEDS)
    alt = first(ALTITUDES)

    common = {"ground_speed_kt": ground, "airspeed_kt": speed, "altitude_ft": alt}
    if radio is not None:
        return SegmentInputs(radio_altitude_ft=radio, source="derived:radio-altitude", **common)
    if ground is not None:
        return SegmentInputs(source="derived:ground-speed", **common)
    return SegmentInputs(source="derived:airspeed", **common)


def segment(
    inputs: SegmentInputs,
    tail_number: str | None = None,
    flight_prefix: str = "F",
) -> list[FlightSegment]:
    """Find the flights in one recording."""
    if inputs.on_ground is not None:
        airborne = ~_debounce(inputs.on_ground)
    elif inputs.radio_altitude_ft is not None:
        radio = np.nan_to_num(inputs.radio_altitude_ft, nan=0.0)
        # A radio altimeter reads nothing useful above a few thousand feet
        # and pins at its maximum, so "not near the ground" is the test --
        # anything that is not clearly below the threshold counts as flying.
        airborne = _debounce(radio > AIRBORNE_FT)
    elif inputs.ground_speed_kt is not None:
        airborne = _debounce(np.nan_to_num(inputs.ground_speed_kt, nan=0.0) > ROLLING_KT)
    elif inputs.airspeed_kt is not None:
        airborne = _debounce(np.nan_to_num(inputs.airspeed_kt, nan=0.0) > TAKEOFF_KT)
    else:
        raise ValueError(
            "segmentation needs an air/ground discrete, a radio altitude, a "
            f"ground speed or an airspeed; none was decoded ({inputs.source})"
        )

    n = airborne.size
    edges = np.flatnonzero(np.diff(np.r_[False, airborne, False]))
    starts, stops = edges[::2], edges[1::2]

    segments: list[FlightSegment] = []
    for i, (takeoff, touchdown) in enumerate(zip(starts, stops), start=1):
        if touchdown - takeoff < MIN_FLIGHT_S:
            continue
        # Include the taxi either side: from the previous landing (or the
        # start of file) to the next takeoff (or the end).
        first = int(stops[i - 2]) if i >= 2 else 0
        last = int(starts[i]) - 1 if i < starts.size else n - 1
        # np.diff gives an exclusive stop; the last airborne frame is the one
        # before it. Off by one here is off by one second on every landing.
        last_airborne = min(int(touchdown) - 1, n - 1)

        # A run that touches a file boundary means the recording started or
        # stopped mid-air, so the rotation or the touchdown is not in this
        # file. Reporting frame 0 as a takeoff would invent an event, and
        # every rotation statistic computed from it would be wrong.
        takeoff_second = None if takeoff == 0 else int(takeoff)
        touchdown_second = None if last_airborne >= n - 1 else last_airborne

        segments.append(
            FlightSegment(
                flight_id=f"{flight_prefix}{i:02d}",
                tail_number=tail_number,
                first_second=first,
                last_second=min(max(last, last_airborne), n - 1),
                takeoff_second=takeoff_second,
                touchdown_second=touchdown_second,
            )
        )

    if not segments:
        # Airborne throughout, or never: either way the file is one segment
        # whose boundaries fall outside it.
        segments.append(
            FlightSegment(
                flight_id=f"{flight_prefix}01",
                tail_number=tail_number,
                first_second=0,
                last_second=n - 1,
                takeoff_second=None,
                touchdown_second=None,
            )
        )
    return segments


def segment_decoded(decoded) -> list[FlightSegment]:
    """Segment straight from a decode."""
    return segment(
        inputs_from_decoded(decoded),
        tail_number=decoded.report.container_metadata.get("tail_number"),
    )
