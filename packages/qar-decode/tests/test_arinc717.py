"""Round-trip tests for the reference decoder."""

from __future__ import annotations

import numpy as np

from qar_decode.arinc717 import (
    SYNC_WORDS,
    Parameter,
    WordPart,
    build_frames,
    decode_parameter,
    find_sync,
    superframe_counter,
    unpack_padded,
)
from qar_decode.arinc717 import _synthesise  # noqa: PLC2701


def test_sync_locks_on_clean_recording() -> None:
    data, _ = _synthesise()
    sync = find_sync(unpack_padded(data))
    assert sync is not None
    assert sync.subframe_size == 64
    assert sync.first_subframe == 1
    assert sync.confidence == 1.0


def test_parameter_round_trip() -> None:
    data, truth = _synthesise()
    words = unpack_padded(data)
    sync = find_sync(words)
    frames, valid = build_frames(words, sync)

    altitude = Parameter(
        name="ALTITUDE_STD",
        parts=(WordPart(subframe=0, word=12, lsb=1, length=12),),
        scale=4.0,
        units="ft",
    )
    values, mask = decode_parameter(frames, valid, altitude)

    assert mask.all()
    np.testing.assert_allclose(values, truth)


def test_corrupt_subframe_is_flagged_not_dropped() -> None:
    """Timing must survive corruption. Dropping a subframe shifts every
    later sample, which is the worst possible bug in flight data."""
    data, _ = _synthesise()
    words = unpack_padded(data)
    sync = find_sync(words)
    _, valid_clean = build_frames(words, sync)

    corrupted = words.copy()
    corrupted[64 * 5] = 0x000
    frames, valid = build_frames(corrupted, sync)

    assert valid.sum() == valid_clean.sum() - 1
    assert frames.shape == (20, 4, 64)


def test_superframe_counter_cycles() -> None:
    data, _ = _synthesise()
    words = unpack_padded(data)
    sync = find_sync(words)
    frames, _ = build_frames(words, sync)
    counter = superframe_counter(frames, subframe=2, word=5)
    assert counter[0] == 0
    assert counter[16] == 0


def test_sync_words_are_the_standard_patterns() -> None:
    assert SYNC_WORDS == (0x247, 0x5B8, 0xA47, 0xDB8)
