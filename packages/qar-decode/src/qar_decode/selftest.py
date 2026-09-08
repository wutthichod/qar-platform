"""Offline proof that the decoder works, end to end.

Synthesises a recording whose contents are known exactly, then puts it
through the same path a real file takes:

    container.unwrap -> frames.build -> parameters.decode

and checks the values come back. No network, no credentials, no sample
files, so it is usable as a container health check and as the first thing
CI runs against a freshly built image.

It deliberately exercises the *live* modules. A self-test that checks a
reference implementation nothing calls proves only that the reference
implementation still works.

    python -m qar_decode.selftest
"""

from __future__ import annotations

import numpy as np

from qar_decode import frames as frames_mod
from qar_decode import parameters as params_mod
from qar_decode.arinc717 import SYNC_WORDS
from qar_decode.container import unwrap
from qar_decode.fap.ags import AcquiredParameter, BitPart, Conversion, FrameLayout, Sample

SUBFRAME_WORDS = 64
N_FRAMES = 32

LAYOUT = FrameLayout(
    name="SELFTEST",
    data_class="QAR",
    subframe_words=SUBFRAME_WORDS,
    frames_per_superframe=16,
    sfc_subframe=1,
    sfc_word=4,
    sfc_bit_lsb=1,
    sfc_bit_length=4,
    dfc_subframe=2,
    dfc_word=5,
    dfc_bit_lsb=1,
    dfc_bit_length=12,
    has_superframe=True,
)

# 4 Hz, one 12-bit word in every subframe, four feet per count.
ALTITUDE = AcquiredParameter(
    mnemonic="ALT_STD",
    name="SELFTEST_ALTITUDE",
    unit="ft",
    stated_rate=4,
    signed=False,
    superframe=False,
    slope=1.0,
    offset=0.0,
    samples=tuple(
        Sample(subframe=sf, index=1, parts=(BitPart(word=12, src_lsb=1, length=12),))
        for sf in (1, 2, 3, 4)
    ),
    conversions=(Conversion(coefficients=(0.0, 4.0)),),
    min_op=-2500.0,
    max_op=50000.0,
)


def _synthesise() -> tuple[bytes, np.ndarray]:
    """A recording with sync, a superframe counter and a known altitude ramp.

    Raw counts are chosen first so the expected values are exactly
    representable; otherwise the check measures quantisation rather than the
    decoder.
    """
    rng = np.random.default_rng(717)
    words = rng.integers(0, 4096, size=(N_FRAMES, 4, SUBFRAME_WORDS)).astype(np.uint16)
    for sf in range(4):
        words[:, sf, 0] = SYNC_WORDS[sf]

    raw = ((np.arange(N_FRAMES * 4) * 6) % 4096).astype(np.uint16)
    words[:, :, 11] = raw.reshape(N_FRAMES, 4)
    words[:, 0, 3] = (np.arange(N_FRAMES) % 16).astype(np.uint16)

    # Little-endian 12-in-16, which is what container.detect calls "wgl".
    return words.ravel().astype("<u2").tobytes(), raw.astype(np.float64) * 4.0


def run(verbose: bool = True) -> int:
    def say(message: str) -> None:
        if verbose:
            print(message, flush=True)

    say("qar_decode self-test")
    data, truth = _synthesise()

    recording = unwrap(data)
    assert recording.container == "wgl", recording.container
    say(f"  container: {recording.container}, {recording.words.size} words")

    fs = frames_mod.build(recording.words, LAYOUT)
    assert fs.sync.subframe_size == SUBFRAME_WORDS, fs.sync
    assert fs.sync.confidence == 1.0, fs.sync
    assert fs.n_frames == N_FRAMES, fs.n_frames
    assert fs.integrity == 1.0, fs.integrity
    say(f"  sync: {fs.sync.subframe_size} words at offset {fs.sync.offset}, "
        f"confidence {fs.sync.confidence:.2f}")

    assert fs.sfc[0] == 0 and fs.sfc[16] == 0, fs.sfc[:20]
    say(f"  superframe counter cycles over {fs.n_frames} frames "
        f"({fs.duration_s:g}s)")

    series = params_mod.decode(fs, ALTITUDE)
    assert series.samples_per_frame == 4, series.samples_per_frame
    assert series.rate_hz == 1.0, series.rate_hz   # 4 per frame, frame = 4 s
    assert series.valid.all(), "samples went missing"
    if not np.allclose(series.values, truth):
        worst = int(np.argmax(np.abs(series.values - truth)))
        raise AssertionError(
            f"altitude mismatch at {worst}: "
            f"got {series.values[worst]}, expected {truth[worst]}"
        )
    say(f"  decoded {series.values.size} altitude samples at "
        f"{series.rate_hz:g} Hz, round-trip exact")

    # Corruption must be flagged, not dropped: dropping a subframe shifts
    # every later sample, which is the worst possible bug in flight data.
    corrupted = recording.words.copy()
    corrupted[SUBFRAME_WORDS * 5] = 0x000
    dirty = frames_mod.build(corrupted, LAYOUT)
    assert dirty.valid.sum() == fs.valid.sum() - 1, "corruption not detected"
    assert dirty.n_frames == fs.n_frames, "timing not preserved"
    say("  corrupt subframe flagged, timing preserved")

    say("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
