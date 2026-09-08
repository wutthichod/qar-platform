"""Regression against the real sample set.

Skipped unless the POC samples are present -- they are operationally
sensitive and are not in the repository. Point QAR_SAMPLES at a checkout of
`qar-data` to run them.

These numbers were established by decoding the files and checking the result
against physics and against the recorders' own metadata: vertical
acceleration averaging 1 g, N1 topping out at takeoff power, the superframe
carrying the tail number and route in ASCII. They are here so that a change
which quietly breaks one of those has to break a test first.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from qar_decode.container import crate, unwrap_path
from qar_decode.decode import decode_file
from qar_decode.fap import load_fap
from qar_decode.segment import inputs_from_decoded, segment_decoded

POC = Path(os.environ.get("QAR_SAMPLES", Path(__file__).resolve().parents[4] / "qar-data"))
SAMPLES = POC / "QAR Samples"
FAPS = POC / "Fap"

pytestmark = pytest.mark.skipif(
    not SAMPLES.is_dir(), reason=f"sample set not present at {SAMPLES}"
)

A350 = SAMPLES / "A350" / "QAR00012000220322111311.20220327121517.HS-THJ.pmf"
B777 = SAMPLES / "B777" / "HS-TTA_20220925065842.wgl" / "raw.dat"
B787 = SAMPLES / "B787" / "TGHS-TQA_CPL__20230108043305+0000-[SMT-TG-ML18].zip"


@pytest.fixture(scope="module")
def a350():
    if not A350.exists():
        pytest.skip("A350 sample absent")
    fap = load_fap(FAPS / "CS350THA22", only={
        "ALT_STD", "VRTG", "N1_1", "HEADING", "PITCH_ANG_CAP", "LATG",
        "RALT1", "GS", "YEAR", "MONTH", "DAY", "UTC_HOUR", "UTC_MIN", "UTC_SEC",
    })
    return decode_file(A350, fap)


def test_a350_container_unwraps_three_layers() -> None:
    if not A350.exists():
        pytest.skip("A350 sample absent")
    rec = unwrap_path(A350)
    assert rec.container == "pmf"
    assert rec.word_order == "big"
    assert rec.metadata["tail_number"] == "HS-THJ"
    assert rec.metadata["flight_number"] == "TG0341"


def test_a350_syncs_perfectly(a350) -> None:
    r = a350.report
    assert r.subframe_words == 1024
    assert r.sync_offset == 2046
    assert r.sync_confidence == 1.0
    assert r.frame_integrity == 1.0
    assert r.n_frames == 5505


def test_a350_values_are_physical(a350) -> None:
    def stats(name):
        s = a350.series[name]
        good = s.values[s.valid]
        return good.min(), good.max(), good.mean()

    # Vertical acceleration averages 1 g over a flight. Nothing else does.
    _, _, vrtg_mean = stats("VRTG")
    assert vrtg_mean == pytest.approx(1.0, abs=0.02)

    lo, hi, _ = stats("ALT_STD")
    assert 0 <= lo < 1000            # on the ground at each end
    assert 30000 < hi < 45000        # cruise

    _, n1_max, _ = stats("N1_1")
    assert 95 < n1_max <= 105        # takeoff power, in per cent

    hdg_lo, hdg_hi, _ = stats("HEADING")
    assert hdg_lo >= -180.01 and hdg_hi <= 180.01


def test_a350_signedness_is_detected_where_the_fap_omits_it(a350) -> None:
    for name in ("HEADING", "PITCH_ANG_CAP", "LATG"):
        assert a350.series[name].signed, name
    assert not a350.series["ALT_STD"].signed


def test_a350_recorded_utc_beats_the_offload_timestamp(a350) -> None:
    """The container says 2022-03-27, which is when the file was offloaded.
    The recording says 2022-03-22, which is when the aircraft flew -- and
    matches the timestamp embedded in the filename."""
    assert a350.start_time is not None
    assert a350.start_time.strftime("%Y%m%d%H%M") == "202203221113"
    assert "QAR00012000220322111311" in A350.name


def test_a350_native_rates(a350) -> None:
    """Rates in hertz, which means dividing samples-per-frame by four.

    VRTG at 8 Hz and altitude at 1 Hz are the standard QAR rates. The same
    parameters read as 32 Hz and 4 Hz -- what you get by treating a frame as
    a second -- are rates no recorder produces.
    """
    rates = a350.by_rate()
    assert "ALT_STD" in rates[1.0]
    assert "VRTG" in rates[8.0]
    assert "N1_1" in rates[4.0]
    assert a350.series["VRTG"].samples_per_frame == 32
    assert a350.series["ALT_STD"].samples_per_frame == 4


def test_a350_frame_is_four_seconds_by_the_recorded_clock(a350) -> None:
    """The timing model, checked against the aircraft's own UTC.

    Every downstream timestamp, sample rate and duration scales with this.
    Getting it wrong is a silent fourfold error that still produces a
    well-formed file, so it is pinned to the one source that cannot be
    argued with: the clock in the recording.
    """
    n = a350.report.n_frames
    sec = a350.series["UTC_SEC"].values.reshape(n, -1)

    # Four UTC_SEC samples inside one frame, one second apart.
    assert a350.series["UTC_SEC"].samples_per_frame == 4
    np.testing.assert_array_equal(np.diff(sec[0]) % 60, [1, 1, 1])

    # And the clock advances four seconds from one frame to the next.
    hh = a350.series["UTC_HOUR"].values.reshape(n, -1)[:, 0]
    mm = a350.series["UTC_MIN"].values.reshape(n, -1)[:, 0]
    t = hh * 3600 + mm * 60 + sec[:, 0]
    step = np.diff(t)
    step = step[np.isfinite(step) & (np.abs(step) < 100)]
    assert (step == 4.0).all()

    assert a350.report.duration_s == n * 4
    assert abs(a350.report.duration_s - (np.nanmax(t) - np.nanmin(t))) <= 4


def test_a350_segments_into_one_complete_flight(a350) -> None:
    segs = segment_decoded(a350)
    assert len(segs) == 1
    s = segs[0]
    assert s.complete
    assert s.tail_number == "HS-THJ"
    # VTBS -> OPKC, Bangkok to Karachi: between four and six hours airborne.
    assert 4 * 3600 < s.airborne_s < 6 * 3600
    assert (
        0 <= s.first_second <= s.takeoff_second < s.touchdown_second <= s.last_second
    )


def test_a350_superframe_spells_the_tail_number() -> None:
    """The strongest check in the set. Word 586 of subframe 1 carries a
    different character on each frame of the superframe cycle; read at the
    modulos the FAP gives, six of them spell HS-THJ -- which is also what
    the container manifest says, arrived at completely independently."""
    if not A350.exists():
        pytest.skip("A350 sample absent")
    fap = load_fap(FAPS / "CS350THA22",
                   only={f"AC_TAIL_{i}" for i in range(1, 7)})
    decoded = decode_file(A350, fap)
    letters = []
    for i in range(1, 7):
        s = decoded.series[f"AC_TAIL_{i}"]
        codes = s.raw[s.valid].astype(int)
        letters.append(chr(np.bincount(codes).argmax()))
    assert "".join(letters) == "HS-THJ"


def test_b777_decodes_and_segments() -> None:
    if not B777.exists():
        pytest.skip("B777 sample absent")
    fap = load_fap(FAPS / "CS77724", only={"aRALTC", "aCAS1", "aALTSTD1", "VRTG"})
    decoded = decode_file(B777, fap)
    r = decoded.report
    assert r.container == "wgl"
    assert r.subframe_words == 512
    assert r.sync_confidence == 1.0
    assert r.frame_integrity == 1.0
    assert r.n_frames == 11044

    assert inputs_from_decoded(decoded).source == "derived:radio-altitude"
    assert r.duration_s == 11044 * 4          # a frame is four seconds
    seg = segment_decoded(decoded)[0]
    assert seg.complete
    # A 777-300ER long sector: getting on for twelve hours in the air.
    assert 11 * 3600 < seg.airborne_s < 13 * 3600


def test_b777_leading_fill_does_not_defeat_sync() -> None:
    """HS-TKN opens with 5,632 words that are not frames. A sync search
    bounded by one subframe never reaches the first real one."""
    other = SAMPLES / "B777" / "HS-TKN_20220513124029.wgl" / "raw.dat"
    if not other.exists():
        pytest.skip("B777 HS-TKN sample absent")
    fap = load_fap(FAPS / "CS77724", only={"aRALTC"})
    r = decode_file(other, fap).report
    assert r.sync_offset == 6144
    assert r.sync_confidence == 1.0


def test_b787_crate_verifies_and_demuxes() -> None:
    if not B787.exists():
        pytest.skip("B787 sample absent")
    stream, meta = crate.inspect(B787.read_bytes())
    assert meta["digest_verified"] is True
    assert meta["tail_number"] == "HS-TQA"
    assert meta["cpl_map"].startswith("647A-00")
    # One 1 Hz record, five 5 Hz, ten 10 Hz -- the rate structure is in the
    # container before any parameter is extracted.
    assert stream.census[4] == 5 * stream.census[3]
    assert stream.census[5] == 10 * stream.census[3]
    assert stream.resyncs == 0
