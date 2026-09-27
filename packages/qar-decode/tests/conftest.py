"""Fixtures.

Everything here is synthesised. Real flight data is operationally sensitive
and the sample set is not in the repository, so the suite that must always
run builds its own recordings and its own FAP. test_samples.py is the one
place that touches real files, and it skips when they are absent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

SYNC = (0x247, 0x5B8, 0xA47, 0xDB8)

LFRAME = """<?xml version="1.0" encoding="utf-16"?>
<FRA FRA_ACQ_CYCLE_NUMBER="0" FRA_DATA_CLASS="QAR" FRA_DFC_BIT_LENGTH="12"
 FRA_DFC_BIT_SOURCE_LSB="1" FRA_DFC_LOC_F="2" FRA_DFC_LOC_W="5"
 FRA_FRAME_FORMAT="0" FRA_NAME="TEST64" FRA_NB_FRAMES_PER_SUPERFRAME="16"
 FRA_SFC_BIT_LENGTH="4" FRA_SFC_BIT_SOURCE_LSB="1" FRA_SFC_LOC_F="1"
 FRA_SFC_LOC_W="4" FRA_SUBFRAME_WORD_NB="64" FRA_SUPERFRAME_TYPE="1" />
"""


def _lcp(word, length, src_lsb=1, tgt_lsb=0, subframe="ALL", sample=1,
         part=1, modulo=0, kind="REGULAR"):
    return (
        f'<LCP LCP_ACQ_CYCLE_NUMBER="0" LCP_BIT_LENGTH="{length}" '
        f'LCP_BIT_SOURCE_LSB="{src_lsb}" LCP_BIT_TARGET_LSB="{tgt_lsb}" '
        f'LCP_MODULO="{modulo}" LCP_PART_NUMBER="{part}" LCP_RATE="1" '
        f'LCP_SAMPLE_NUMBER="{sample}" LCP_SUBFRAME="{subframe}" '
        f'LCP_TYPE="{kind}" LCP_WORD_NB="{word}" PSA_ID="0" />'
    )


def _acquired(mnemonic, locations, *, unit="", rate=4, coeff=None,
              min_op=None, max_op=None, superframe=0, signed=0, txd=(), conv=""):
    euc = ""
    if coeff is not None:
        mantissa, exponent = coeff
        euc = (f'<EUC EUC_COEFF_1="{mantissa}" EUC_COEFF_EXP_1="{exponent}" '
               f'EUC_MAX="999999" EUC_MIN="-999999" />')
    ranges = ""
    if min_op is not None:
        ranges = f'PRM_MIN_OP_RANGE="{min_op}" PRM_MAX_OP_RANGE="{max_op}"'
    text = "".join(
        f'<TXD TXD_RAW_DATA_VALUE="{v}" TXD_DESCRIPTION="{d}" />' for v, d in txd
    )
    return (
        '<?xml version="1.0" encoding="utf-16"?>\n'
        f'<PRA EUT_ID="1" PRA_CONV_CONF="{conv}" PRA_NB_FRAMES_PER_SCYCLE="16" PRA_OFFSET="0" '
        f'PRA_SLOPE="1" PRA_SUPERFRAME="{superframe}" '
        f'PRM_MNEMONIC="{mnemonic}" PRM_NAME="{mnemonic}_LONG" '
        f'PRM_PARAMETER_SIGNED="{signed}" PRM_RATE="{rate}" '
        f'PRM_UNIT="{unit}" {ranges}>'
        f'{euc}{text}{"".join(locations)}</PRA>'
    )


@pytest.fixture
def fap_dir(tmp_path: Path) -> Path:
    """A small FAP exercising every location shape that matters."""
    root = tmp_path / "CSTEST"
    (root / "AcquiredParameters").mkdir(parents=True)
    (root / "TEST64.lframe").write_text(LFRAME, encoding="utf-8-sig")

    write = lambda name, body: (                                     # noqa: E731
        root / "AcquiredParameters" / f"{name}_USR.lacquired"
    ).write_text(body, encoding="utf-8-sig")

    # 4 Hz, one 12-bit word in every subframe, scale 4.
    write("ALT", _acquired("ALT", [_lcp(word=12, length=12)],
                           unit="ft", rate=4, coeff=(4, 0),
                           min_op=-2500, max_op=50000))
    # 8 Hz: two samples per subframe.
    write("VRT", _acquired(
        "VRT",
        [_lcp(word=20, length=12, sample=1), _lcp(word=40, length=12, sample=2)],
        unit="g", rate=8, coeff=(3.90625, -3), min_op=-3, max_op=6,
    ))
    # Split across subframes: low 8 bits in subframe 1, high 4 in subframe 2.
    write("SPLIT", _acquired(
        "SPLIT",
        [_lcp(word=30, length=8, subframe="1", part=1),
         _lcp(word=30, length=4, subframe="2", part=2)],
        rate=1, min_op=0, max_op=4095,
    ))
    # Two's complement in practice, unsigned in the FAP.
    write("PITCH", _acquired("PITCH", [_lcp(word=25, length=12)],
                             unit="deg", rate=4, coeff=(1.7578125, -1),
                             min_op=-90, max_op=90))
    # Superframe: present only when the counter reads 5.
    write("SUPER", _acquired(
        "SUPER", [_lcp(word=50, length=12, subframe="1", modulo=5, kind="SUPERFRAME")],
        rate=1, superframe=1,
    ))
    # A discrete with labels.
    write("GEAR", _acquired("GEAR", [_lcp(word=18, length=1)], rate=4,
                            txd=((0, "Not on Ground"), (1, "On Ground"))))
    return root


def synth_recording(n_frames=64, size=64, big_endian=False, lead=0,
                    pad_nibble=0x0):
    """A recording with known contents, in either word order.

    Returns (bytes, truth) where truth maps a parameter name to the exact
    engineering values it must decode to.
    """
    rng = np.random.default_rng(717)
    words = rng.integers(0, 4096, size=(n_frames, 4, size)).astype(np.uint16)
    for sf in range(4):
        words[:, sf, 0] = SYNC[sf]

    alt_raw = (np.arange(n_frames * 4) * 7 % 4000).astype(np.uint16)
    words[:, :, 11] = alt_raw.reshape(n_frames, 4)

    vrt_a = (np.arange(n_frames * 4) * 3 % 300 + 200).astype(np.uint16)
    vrt_b = (np.arange(n_frames * 4) * 5 % 300 + 200).astype(np.uint16)
    words[:, :, 19] = vrt_a.reshape(n_frames, 4)
    words[:, :, 39] = vrt_b.reshape(n_frames, 4)

    # Pitch as an aircraft actually flies it: a few degrees either side of
    # level, so the negative half is two's complement near 0xFFF rather
    # than an unused corner of the range.
    pitch_deg = 12.0 * np.sin(np.arange(n_frames * 4) / 37.0)
    pitch_raw = (np.rint(pitch_deg / 0.17578125).astype(np.int64) % 4096).astype(np.uint16)
    words[:, :, 24] = pitch_raw.reshape(n_frames, 4)

    split_lo = (np.arange(n_frames) % 256).astype(np.uint16)
    split_hi = (np.arange(n_frames) % 16).astype(np.uint16)
    words[:, 0, 29] = split_lo
    words[:, 1, 29] = split_hi

    # Superframe counter in subframe 1 word 4, cycling 0..15.
    sfc = (np.arange(n_frames) % 16).astype(np.uint16)
    words[:, 0, 3] = sfc
    super_raw = (np.arange(n_frames) * 13 % 4096).astype(np.uint16)
    words[:, 0, 49] = super_raw

    gear = (np.arange(n_frames * 4) % 2).astype(np.uint16)
    words[:, :, 17] = gear.reshape(n_frames, 4)

    flat = words.ravel().astype(np.uint16)
    if pad_nibble:
        flat = flat | (pad_nibble << 12)
    if lead:
        flat = np.r_[np.full(lead, 0xFFFF if pad_nibble else 0x0000, np.uint16), flat]

    data = flat.astype(">u2" if big_endian else "<u2").tobytes()

    truth = {
        "ALT": alt_raw.astype(np.float64) * 4.0,
        "VRT": np.stack([vrt_a, vrt_b], axis=1).ravel().astype(np.float64) * 3.90625e-3,
        "SPLIT": (split_lo | (split_hi << 8)).astype(np.float64),
        "SFC": sfc,
        "SUPER_RAW": super_raw,
        "GEAR": gear.astype(np.float64),
        "PITCH": np.rint(pitch_deg / 0.17578125) * 0.17578125,
    }
    return data, truth
