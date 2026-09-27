"""Fields the FAP declares as BCD or packed text, and the recorded clock.

Both old failure modes produced plausible values: a BCD year read as binary
is a real-looking 2034, and a start time taken from the container is a
real-looking date days after the flight. So the tests pin the specific wrong
answer the old behaviour gave, not only the right one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from conftest import LFRAME, _acquired, _lcp, synth_recording
from qar_decode import frames as frames_mod
from qar_decode import parameters as params_mod
from qar_decode.decode import _resolve_start_time
from qar_decode.fap import load_fap
from qar_decode.parameters import Series

AIRBUS = ("YEAR", "MONTH", "DAY", "UTC_HOUR", "UTC_MIN", "UTC_SEC")
BOEING = ("aYEAR", "aMONTH", "aDAY", "aGMTH", "aGMTM", "aGMTS")
T0 = datetime(2022, 9, 25, 6, 58, 42, tzinfo=timezone.utc)


def _fap(tmp_path: Path, **params: str) -> Path:
    root = tmp_path / "CSENC"
    (root / "AcquiredParameters").mkdir(parents=True)
    (root / "TEST64.lframe").write_text(LFRAME, encoding="utf-8-sig")
    for name, body in params.items():
        (root / "AcquiredParameters" / f"{name}_USR.lacquired").write_text(
            body, encoding="utf-8-sig"
        )
    return root


def _words(n_frames: int) -> np.ndarray:
    data, _ = synth_recording(n_frames=n_frames)
    return (np.frombuffer(data, dtype="<u2") & 0x0FFF).copy().reshape(n_frames, 4, 64)


@pytest.fixture
def encoded(tmp_path):
    root = _fap(
        tmp_path,
        YEAR=_acquired("YEAR", [_lcp(word=55, length=8)], conv="44"),
        DAY=_acquired("DAY", [_lcp(word=56, length=6)], conv="24"),
        BAD=_acquired("BAD", [_lcp(word=57, length=8)], conv="44"),
        SCALED=_acquired("SCALED", [_lcp(word=58, length=8)], conv="44", coeff=(1, -1)),
        CHAR=_acquired("CHAR", [_lcp(word=59, length=7)], conv="7"),
        PAIR=_acquired("PAIR", [_lcp(word=60, length=7, part=1),
                                _lcp(word=61, length=7, part=2)], conv="7"),
        PLAIN=_acquired("PLAIN", [_lcp(word=62, length=8)]),
        DOT=_acquired("DOT", [_lcp(word=62, length=8)], conv="."),
    )
    words = _words(16)
    words[:, :, 54] = 0x22          # YEAR   BCD 22, binary 34
    words[:, :, 55] = 0b10_0101     # DAY    BCD 25, binary 37
    words[:, :, 56] = 0x2A          # BAD    a nibble of 10 is not a digit
    words[:, :, 57] = 0x22          # SCALED BCD 22, then x 0.1
    words[:, :, 58] = ord("H")      # CHAR
    words[:, :, 59] = ord("S")      # PAIR   low part  -> second character
    words[:, :, 60] = ord("H")      # PAIR   high part -> first character
    words[:, :, 61] = 0x22          # PLAIN / DOT
    fap = load_fap(root)
    return frames_mod.build(words.ravel(), fap.frame), fap


# --- reading PRA_CONV_CONF ---------------------------------------------------

def test_conv_conf_is_read_as_digit_widths(encoded) -> None:
    _, fap = encoded
    assert fap["YEAR"].digit_widths == (4, 4)
    assert fap["YEAR"].encoding == "bcd"
    assert fap["DAY"].field_widths == (2, 4)
    assert fap["CHAR"].encoding == "text"
    assert fap["PAIR"].field_widths == (7, 7)       # "7" tiles a 14-bit field
    assert fap["PLAIN"].encoding == "binary"
    assert fap["DOT"].digit_widths == ()            # "." is a plain number
    assert fap["DOT"].encoding == "binary"


def test_a_code_split_across_parameters_fits_from_the_front(tmp_path) -> None:
    """ "7777" on a 7-bit field is one letter of a four-letter ICAO code."""
    root = _fap(tmp_path, C=_acquired("C", [_lcp(word=59, length=7)], conv="7777"))
    assert load_fap(root)["C"].field_widths == (7,)


# --- BCD ---------------------------------------------------------------------

def test_bcd_year_is_22_not_34(encoded) -> None:
    fs, fap = encoded
    s = params_mod.decode(fs, fap["YEAR"])
    assert s.encoding == "bcd"
    assert np.all(s.values == 22)
    assert np.all(s.raw == 34)                      # what binary reading gave


def test_bcd_with_a_two_bit_tens_digit(encoded) -> None:
    fs, fap = encoded
    assert np.all(params_mod.decode(fs, fap["DAY"]).values == 25)


def test_a_nibble_above_nine_invalidates_the_sample(encoded) -> None:
    fs, fap = encoded
    s = params_mod.decode(fs, fap["BAD"])
    assert not s.valid.any()
    assert np.isnan(s.values).all()


def test_conversion_runs_on_the_decimal_value(encoded) -> None:
    fs, fap = encoded
    np.testing.assert_allclose(params_mod.decode(fs, fap["SCALED"]).values, 2.2)


def test_plain_binary_is_untouched(encoded) -> None:
    fs, fap = encoded
    assert np.all(params_mod.decode(fs, fap["PLAIN"]).values == 34)
    assert np.all(params_mod.decode(fs, fap["DOT"]).values == 34)


# --- packed text -------------------------------------------------------------

def test_packed_characters_become_text(encoded) -> None:
    fs, fap = encoded
    char = params_mod.decode(fs, fap["CHAR"])
    assert char.encoding == "text"
    assert set(char.text) == {"H"}
    # Most significant sub-field first: the high part is the first letter.
    assert set(params_mod.decode(fs, fap["PAIR"]).text) == {"HS"}


def test_an_all_nul_field_is_missing_not_empty_text(encoded) -> None:
    fs, fap = encoded
    fs.data[:, :, 58] = 0
    s = params_mod.decode(fs, fap["CHAR"])
    assert not s.valid.any()
    assert all(t is None for t in s.text)


def test_text_is_written_as_a_string_column(tmp_path) -> None:
    pa = pytest.importorskip("pyarrow")
    from qar_decode.arrow import tables
    from qar_decode.decode import decode_bytes

    root = _fap(tmp_path, CHAR=_acquired("CHAR", [_lcp(word=59, length=7)], conv="7"))
    words = _words(16)
    words[:, :, 58] = ord("H")
    fap = load_fap(root)
    decoded = decode_bytes(words.ravel().astype("<u2").tobytes(), fap)
    (table,) = tables(decoded, fap).values()
    column = table.schema.field("CHAR")
    assert column.type == pa.string()
    assert column.metadata[b"encoding"] == b"text"
    assert set(table.column("CHAR").to_pylist()) == {"H"}


# --- the recorded clock ------------------------------------------------------

def _clock(start: datetime, n: int, names: tuple[str, ...],
           rate: float = 1.0) -> dict[str, Series]:
    """The six UTC fields, one sample per second, from a clock running at
    ``rate`` recorded seconds per frame-count second."""
    stamps = [start + timedelta(seconds=int(i * rate)) for i in range(n)]
    columns = (
        [t.year % 100 for t in stamps], [t.month for t in stamps],
        [t.day for t in stamps], [t.hour for t in stamps],
        [t.minute for t in stamps], [t.second for t in stamps],
    )
    out = {}
    for name, column in zip(names, columns):
        v = np.array(column, dtype=float)
        out[name] = Series(name, v, np.ones(n, bool), 4, None, v.astype(np.int64), False, 8)
    return out


def test_recorded_clock_gives_the_start_and_says_so() -> None:
    st = _resolve_start_time(_clock(T0, 300, BOEING), {"recorded_at": "2022-10-01T06:46:03"}, 300)
    assert st.value == T0
    assert st.source == "recorded_utc"
    assert st.agreement == 1.0
    assert st.max_drift_s == 0


def test_a_corrupt_first_sample_does_not_move_the_start() -> None:
    """Taking sample [0] would have put this flight on the 9th at 03:58."""
    series = _clock(T0, 300, AIRBUS)
    series["UTC_HOUR"].values[0] = 3
    series["DAY"].values[0] = 9
    st = _resolve_start_time(series, {}, 300)
    assert st.value == T0
    assert st.source == "recorded_utc"


def test_an_impossible_date_drops_that_second_not_the_clock() -> None:
    """Binary-read BCD gives a 37th of the month. Before, that raised inside
    datetime() and the whole file silently lost its time axis."""
    series = _clock(T0, 300, BOEING)
    series["aDAY"].values[:20] = 37
    st = _resolve_start_time(series, {}, 300)
    assert st.value == T0
    assert st.seconds_checked == 280


def test_the_clock_survives_midnight() -> None:
    start = datetime(2022, 3, 22, 23, 58, 0, tzinfo=timezone.utc)
    st = _resolve_start_time(_clock(start, 300, AIRBUS), {}, 300)
    assert st.value == start
    assert st.agreement == 1.0


def test_date_fields_recorded_once_per_frame_are_carried_forward() -> None:
    series = _clock(T0, 300, AIRBUS)
    for name in ("YEAR", "MONTH", "DAY"):
        v = series[name].values[::4].copy()
        series[name] = Series(name, v, np.ones(v.size, bool), 1, None,
                              v.astype(np.int64), False, 8)
    assert _resolve_start_time(series, {}, 300).value == T0


def test_the_container_fallback_is_labelled_not_silent() -> None:
    st = _resolve_start_time({}, {"recorded_at": "2022-03-27T12:15:30"}, 300)
    assert st.source == "container:recorded_at"
    assert st.value == datetime(2022, 3, 27, 12, 15, 30, tzinfo=timezone.utc)
    assert any("not the aircraft clock" in note for note in st.notes)


def test_a_clock_that_disagrees_with_itself_is_not_trusted() -> None:
    series = _clock(T0, 300, AIRBUS)
    series["UTC_MIN"].values[:] = np.random.default_rng(7).integers(0, 60, 300)
    st = _resolve_start_time(series, {"recorded_at": "2022-03-27T12:15:30"}, 300)
    assert st.source == "container:recorded_at"
    assert any("agree" in note for note in st.notes)


def test_no_clock_and_no_container_is_reported() -> None:
    st = _resolve_start_time({}, {}, 300)
    assert st.value is None
    assert st.source == "none"
    assert st.notes


def test_a_clock_that_drifts_steadily_is_fitted_not_rejected() -> None:
    """Both B777 samples' clocks gain about a second an hour on the frame
    count. A constant-offset model calls that healthy clock inconsistent and
    throws the time axis away; a line fits it."""
    rate = 1 + 1 / 3000
    st = _resolve_start_time(_clock(T0, 12_000, BOEING, rate=rate), {}, 12_000)
    assert st.source == "recorded_utc"
    assert st.value == T0
    # The clock records whole seconds, so the rate can only be resolved to
    # about one second over the recording. That is the property that matters.
    assert abs((st.clock_rate - rate) * 12_000) < 1
    assert st.agreement >= 0.99
    assert any("s/h" in note for note in st.notes)


def test_a_slow_date_field_just_after_midnight_is_not_a_day_of_drift() -> None:
    """The date is sampled once per frame, the time every second, so for a
    few seconds after midnight the date still reads yesterday. HS-TTA has
    exactly one such second; unhandled, it reported 86,400 s of drift."""
    start = datetime(2022, 9, 24, 23, 57, 59, tzinfo=timezone.utc)   # midnight at t=121
    series = _clock(start, 300, AIRBUS)
    for name in ("YEAR", "MONTH", "DAY"):
        v = series[name].values[::4].copy()
        series[name] = Series(name, v, np.ones(v.size, bool), 1, None,
                              v.astype(np.int64), False, 8)
    st = _resolve_start_time(series, {}, 300)
    assert st.value == start
    assert st.max_drift_s <= 1
    assert not any("strays" in note for note in st.notes)
