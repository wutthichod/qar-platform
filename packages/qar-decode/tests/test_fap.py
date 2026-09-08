"""The FAP reader: does it read what the document says?"""

from __future__ import annotations

from qar_decode.fap import load_fap


def test_frame_geometry(fap_dir) -> None:
    fap = load_fap(fap_dir)
    assert fap.frame.subframe_words == 64
    assert fap.frame.frames_per_superframe == 16
    assert (fap.frame.sfc_subframe, fap.frame.sfc_word) == (1, 4)
    assert fap.frame.has_superframe


def test_documents_are_utf8_despite_declaring_utf16(fap_dir) -> None:
    """The prolog lies. Trusting it yields mojibake, not an exception, which
    is why this is worth an explicit test."""
    raw = (fap_dir / "AcquiredParameters" / "ALT_USR.lacquired").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b'encoding="utf-16"' in raw
    assert load_fap(fap_dir)["ALT"].mnemonic == "ALT"


def test_rate_comes_from_locations_not_the_stated_field(fap_dir) -> None:
    fap = load_fap(fap_dir)
    assert fap["ALT"].samples_per_frame == 4     # ALL subframes x 1 sample
    assert fap["VRT"].samples_per_frame == 8     # ALL subframes x 2 samples
    assert fap.rate_disagreements() == []


def test_prm_rate_is_samples_per_frame_not_hertz(fap_dir) -> None:
    """A frame is four seconds. PRM_RATE=4 is 1 Hz, not 4 Hz -- reading it
    as hertz overstates every rate in the FAP fourfold."""
    fap = load_fap(fap_dir)
    assert fap["ALT"].samples_per_frame == 4
    assert fap["ALT"].rate_hz == 1.0
    assert fap["VRT"].rate_hz == 2.0


def test_conversion_is_mantissa_times_power_of_ten(fap_dir) -> None:
    fap = load_fap(fap_dir)
    assert fap["ALT"].conversions[0].coefficients == (0.0, 4.0)
    assert fap["VRT"].conversions[0].coefficients[1] == 3.90625e-3


def test_parts_in_different_subframes_stay_one_sample(fap_dir) -> None:
    """SPLIT keeps eight bits in subframe 1 and four in subframe 2.

    Reading that as two parameters is the failure this guards: both halves
    decode to plausible numbers, so nothing downstream notices."""
    param = load_fap(fap_dir)["SPLIT"]
    assert param.samples_per_frame == 1
    assert len(param.samples) == 1
    assert [p.subframe for p in param.samples[0].parts] == [1, 2]
    assert param.bits == 12


def test_target_lsb_zero_means_continue_not_bit_zero(fap_dir) -> None:
    parts = load_fap(fap_dir)["SPLIT"].samples[0].parts
    assert [p.tgt_lsb for p in parts] == [0, 0]
    assert [p.length for p in parts] == [8, 4]


def test_superframe_modulo_is_kept(fap_dir) -> None:
    param = load_fap(fap_dir)["SUPER"]
    assert param.superframe
    assert param.samples[0].modulo == 5


def test_discrete_labels(fap_dir) -> None:
    param = load_fap(fap_dir)["GEAR"]
    assert param.is_discrete
    assert param.discretes == {0: "Not on Ground", 1: "On Ground"}


def test_only_filter_avoids_parsing_everything(fap_dir) -> None:
    fap = load_fap(fap_dir, only={"ALT"})
    assert set(fap.parameters) == {"ALT"}
