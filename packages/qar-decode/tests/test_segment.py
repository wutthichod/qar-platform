"""Segmentation, and the debounce that makes it usable."""

from __future__ import annotations

import numpy as np
import pytest

from qar_decode.segment import (
    FlightSegment,
    SegmentInputs,
    _debounce,
    segment,
)


def _profile(taxi_out=300, airborne=3600, taxi_in=240) -> np.ndarray:
    return np.r_[
        np.zeros(taxi_out, bool), np.ones(airborne, bool), np.zeros(taxi_in, bool)
    ]


def test_debounce_removes_short_interior_runs() -> None:
    flags = _profile()
    flags[1000:1003] = False           # a bounce
    assert _debounce(flags).sum() == _profile().sum()


def test_debounce_repeats_until_stable() -> None:
    """Three bursts two seconds apart are one nineteen-second event, not
    three. Flipping one short run merges its neighbours and can leave a run
    that is still too short, so the pass has to repeat until nothing moves.
    A single pass leaves exactly the chatter it was written to remove."""
    flags = np.zeros(600, bool)
    flags[100:105] = True
    flags[107:112] = True
    flags[114:119] = True
    out = _debounce(flags, hold=15)
    assert out[100:119].all()
    assert out.sum() == 19


def test_debounce_keeps_runs_at_the_file_edges() -> None:
    """A short run at a boundary is a truncated segment, not noise."""
    flags = np.zeros(600, bool)
    flags[:5] = True
    assert _debounce(flags, hold=15)[:5].all()


def test_ground_discrete_gives_takeoff_and_touchdown() -> None:
    airborne = _profile()
    segs = segment(SegmentInputs(on_ground=~airborne, source="test"))
    assert len(segs) == 1
    s = segs[0]
    assert s.takeoff_frame == 300
    assert s.touchdown_frame == 300 + 3600 - 1
    assert s.complete
    assert s.first_frame == 0
    assert s.last_frame == airborne.size - 1


def test_touchdown_is_the_last_airborne_frame() -> None:
    """np.diff gives an exclusive stop. Off by one here is off by one second
    on every landing in the fleet."""
    airborne = _profile(taxi_out=100, airborne=1000, taxi_in=100)
    s = segment(SegmentInputs(on_ground=~airborne, source="test"))[0]
    assert airborne[s.touchdown_frame]
    assert not airborne[s.touchdown_frame + 1]


def test_radio_altitude_path() -> None:
    airborne = _profile()
    radio = np.where(airborne, 5000.0, 0.0)
    s = segment(SegmentInputs(radio_altitude_ft=radio, source="test"))[0]
    assert s.takeoff_frame == 300
    assert s.complete


def test_ground_speed_path() -> None:
    airborne = _profile()
    speed = np.where(airborne, 400.0, 10.0)
    s = segment(SegmentInputs(ground_speed_kt=speed, source="test"))[0]
    assert s.takeoff_frame == 300


def test_two_sectors_in_one_file() -> None:
    airborne = np.r_[
        np.zeros(200, bool), np.ones(1800, bool), np.zeros(900, bool),
        np.ones(2000, bool), np.zeros(200, bool),
    ]
    segs = segment(SegmentInputs(on_ground=~airborne, source="test"))
    assert [s.flight_id for s in segs] == ["F01", "F02"]
    assert segs[0].takeoff_frame == 200
    assert segs[1].takeoff_frame == 2900
    assert all(s.complete for s in segs)


def test_a_file_that_is_airborne_throughout_is_one_open_segment() -> None:
    """A cruise extract has no takeoff in it. Saying so beats discarding the
    sector or inventing a boundary."""
    segs = segment(SegmentInputs(on_ground=np.zeros(4000, bool), source="test"))
    assert len(segs) == 1
    assert not segs[0].complete
    assert segs[0].takeoff_frame is None
    assert segs[0].touchdown_frame is None


def test_a_recording_that_stops_in_the_air_has_no_touchdown() -> None:
    airborne = np.r_[np.zeros(300, bool), np.ones(3000, bool)]
    s = segment(SegmentInputs(on_ground=~airborne, source="test"))[0]
    assert s.takeoff_frame == 300
    assert s.touchdown_frame is None
    assert not s.complete


def test_frame_indices_stay_inside_the_recording() -> None:
    airborne = _profile(taxi_out=100, airborne=1000, taxi_in=0)
    n = airborne.size
    for s in segment(SegmentInputs(on_ground=~airborne, source="test")):
        assert 0 <= s.first_frame <= s.last_frame < n
        assert s.touchdown_frame is None or s.touchdown_frame < n


def test_no_usable_signal_is_an_error_not_a_guess() -> None:
    with pytest.raises(ValueError, match="air/ground discrete"):
        segment(SegmentInputs(source="nothing decoded"))


def test_segment_reports_durations() -> None:
    s = FlightSegment("F01", "HS-TST", 0, 4139, takeoff_frame=300, touchdown_frame=3899)
    assert s.duration_s == 4140
    assert s.airborne_s == 3599
