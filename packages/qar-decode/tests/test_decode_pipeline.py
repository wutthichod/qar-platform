"""Frames, parameters and the pipeline, against recordings whose contents
are known exactly."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import synth_recording
from qar_decode import frames as frames_mod
from qar_decode import parameters as params_mod
from qar_decode.container import unwrap
from qar_decode.decode import decode_bytes
from qar_decode.fap import load_fap


@pytest.fixture
def built(fap_dir):
    data, truth = synth_recording(n_frames=64)
    fap = load_fap(fap_dir)
    rec = unwrap(data)
    return frames_mod.build(rec.words, fap.frame), fap, truth


def test_frames_locate_and_count(built) -> None:
    fs, _, _ = built
    assert fs.sync.subframe_size == 64
    assert fs.sync.offset == 0
    assert fs.sync.confidence == 1.0
    assert fs.n_frames == 64
    assert fs.integrity == 1.0


def test_sync_survives_a_leading_header(fap_dir) -> None:
    """A recording that opens with thousands of non-frame words must still
    lock. An offset scan bounded by one subframe never reaches it, and the
    failure is indistinguishable from an unreadable file."""
    data, _ = synth_recording(n_frames=64, lead=5000)
    fs = frames_mod.build(unwrap(data).words, load_fap(fap_dir).frame)
    assert fs.sync.offset == 5000
    assert fs.sync.confidence == 1.0


def test_superframe_counter_cycles(built) -> None:
    fs, _, truth = built
    np.testing.assert_array_equal(fs.sfc, truth["SFC"])


def test_regular_parameter_round_trip(built) -> None:
    fs, fap, truth = built
    s = params_mod.decode(fs, fap["ALT"])
    assert s.rate == 4
    np.testing.assert_allclose(s.values, truth["ALT"])


def test_multi_sample_parameter_interleaves_in_time_order(built) -> None:
    """Two samples per subframe must come out subframe-major, sample-minor.
    Getting the order wrong keeps every value and puts none of them at the
    time it was recorded."""
    fs, fap, truth = built
    s = params_mod.decode(fs, fap["VRT"])
    assert s.rate == 8
    np.testing.assert_allclose(s.values, truth["VRT"], rtol=1e-9)


def test_parameter_split_across_subframes(built) -> None:
    fs, fap, truth = built
    s = params_mod.decode(fs, fap["SPLIT"])
    assert s.rate == 1
    np.testing.assert_allclose(s.values, truth["SPLIT"])


def test_two_s_complement_is_detected_from_the_operating_range(built) -> None:
    """PITCH is signed in the aircraft and unsigned in the FAP. Read
    unsigned, every nose-down attitude becomes about +180 degrees."""
    fs, fap, truth = built
    s = params_mod.decode(fs, fap["PITCH"])
    assert s.signed
    np.testing.assert_allclose(s.values, truth["PITCH"], atol=1e-6)
    assert s.values.min() < -11.0


def test_unsigned_parameters_are_left_alone(built) -> None:
    fs, fap, _ = built
    assert not params_mod.decode(fs, fap["ALT"]).signed


def test_superframe_parameter_is_held_across_the_cycle(built) -> None:
    fs, fap, truth = built
    s = params_mod.decode(fs, fap["SUPER"])
    assert s.rate == 1
    # Frames where the counter reads 5 carry the value recorded there.
    at_five = np.flatnonzero(fs.sfc == 5)
    np.testing.assert_allclose(
        s.values[at_five], truth["SUPER_RAW"][at_five].astype(float)
    )
    # The frame after one of those holds the same value rather than a gap.
    follow = at_five[0] + 1
    assert s.values[follow] == s.values[at_five[0]]


def test_all_ones_is_treated_as_no_computed_data(fap_dir) -> None:
    data, _ = synth_recording(n_frames=64)
    words = np.frombuffer(data, dtype="<u2").copy()
    words[64 * 11 : 64 * 11 + 1] = 0  # keep sync intact
    fap = load_fap(fap_dir)
    fs = frames_mod.build(np.frombuffer(data, "<u2") & 0x0FFF, fap.frame)
    fs.data[:, :, 11] = 0x0FFF
    s = params_mod.decode(fs, fap["ALT"])
    assert not s.valid.any()
    assert np.isnan(s.values).all()


def test_corrupt_subframe_is_flagged_not_dropped(fap_dir) -> None:
    """Dropping a subframe shifts every later sample. Timing must survive."""
    data, _ = synth_recording(n_frames=64)
    words = (np.frombuffer(data, dtype="<u2") & 0x0FFF).copy()
    fap = load_fap(fap_dir)
    clean = frames_mod.build(words, fap.frame)
    words[64 * 5] = 0x000
    dirty = frames_mod.build(words, fap.frame)
    assert dirty.valid.sum() == clean.valid.sum() - 1
    assert dirty.n_frames == clean.n_frames


def test_pipeline_reports_what_it_did(fap_dir) -> None:
    data, _ = synth_recording(n_frames=64)
    decoded = decode_bytes(data, load_fap(fap_dir), source="test")
    r = decoded.report
    assert r.container == "wgl"
    assert r.n_frames == 64
    assert r.frame_integrity == 1.0
    assert not r.failed
    assert decoded.by_rate() == {1: ["SPLIT", "SUPER"], 4: ["ALT", "GEAR", "PITCH"],
                                 8: ["VRT"]}
    assert "frame integrity" in r.summary()


def test_unknown_parameter_is_reported_not_raised(fap_dir) -> None:
    data, _ = synth_recording(n_frames=64)
    decoded = decode_bytes(data, load_fap(fap_dir), only={"ALT", "NOPE"})
    assert decoded.report.failed == {"NOPE": "not in FAP"}
    assert decoded.report.decoded == ["ALT"]


def test_discontinuous_parameter_is_flagged(fap_dir) -> None:
    """A mis-mapped continuous parameter usually stays inside its declared
    range, so only the jump test finds it."""
    rng = np.random.default_rng(1)
    data, _ = synth_recording(n_frames=256)
    words = (np.frombuffer(data, dtype="<u2") & 0x0FFF).copy()
    grid = words.reshape(-1, 64)
    grid[:, 24] = rng.integers(0, 4096, size=grid.shape[0])   # PITCH: noise

    decoded = decode_bytes(
        grid.ravel().astype("<u2").tobytes(), load_fap(fap_dir), only={"PITCH", "ALT"}
    )
    flagged = {f.mnemonic for f in decoded.report.suspect}
    assert "PITCH" in flagged
    assert "ALT" not in flagged
