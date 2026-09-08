"""The frame grid: a word stream reshaped into addressable time.

One object, built once per file, that everything downstream indexes into.
Separating it from parameter extraction is what lets a decode report "sync
was fine, the FAP is wrong" instead of one undifferentiated failure.

**The two timing facts everything here rests on**, from ARINC 717:

    a subframe is one second
    a frame is four subframes, so a frame is four seconds

They are stated as constants below rather than left implicit, because
conflating a frame with a second is a silent four-fold error in every
timestamp, sample rate and duration the decoder produces -- and it is
verifiable rather than assumed. The A350 sample carries recorded UTC, and
its clock advances 4.000 s per frame across all 5,504 frame transitions,
with the four UTC_SEC samples inside one frame reading one second apart.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from qar_decode.arinc717 import (
    FRAME_SECONDS,
    SUBFRAME_SECONDS,
    SUBFRAMES_PER_FRAME,
    SYNC_WORDS,
    SyncResult,
    find_sync,
)

# Re-exported: these are ARINC 717 facts and live in that module, but the
# rest of the package reads them off the frame grid they govern.
__all__ = [
    "FRAME_SECONDS",
    "SUBFRAMES_PER_FRAME",
    "SUBFRAME_SECONDS",
    "FrameSet",
    "build",
]


@dataclass
class FrameSet:
    """Located frames, plus the counters that index superframe position."""

    data: np.ndarray          # [frame, subframe, word], uint16 of 12-bit values
    valid: np.ndarray         # [frame, subframe], bool
    sync: SyncResult
    sfc: np.ndarray           # [frame] superframe counter
    dfc: np.ndarray           # [frame] frame counter, as recorded

    @property
    def n_frames(self) -> int:
        return int(self.data.shape[0])

    @property
    def n_subframes(self) -> int:
        """Subframes, which is also the recording's length in seconds."""
        return int(self.data.shape[0] * SUBFRAMES_PER_FRAME)

    @property
    def duration_s(self) -> float:
        return self.n_frames * FRAME_SECONDS

    @property
    def subframe_words(self) -> int:
        return int(self.data.shape[2])

    @property
    def integrity(self) -> float:
        """Fraction of subframes whose sync word was where it should be."""
        return float(self.valid.mean()) if self.valid.size else 0.0

    def word(self, subframe: int, word: int) -> np.ndarray:
        """One word position across every frame. 1-based, as the FAP writes it."""
        return self.data[:, subframe - 1, word - 1]


def build(words: np.ndarray, layout) -> FrameSet:
    """Reshape a word stream into frames and read its counters.

    The layout's subframe size is tried first, then the others. It is a
    strong hint, not an instruction: if the FAP says 1024 words and the file
    syncs at 512, the file wins and the caller can see the disagreement by
    comparing ``sync.subframe_size`` against the layout.
    """
    sizes = (64, 128, 256, 512, 1024, 2048, 4160)
    sizes = (layout.subframe_words,) + tuple(
        s for s in sizes if s != layout.subframe_words
    )

    sync = find_sync(words, candidate_sizes=sizes)
    if sync is None:
        raise ValueError(
            "no ARINC 717 subframe sync found. Either the container was not "
            "fully unwrapped, or the word packing is wrong (endianness is the "
            "usual culprit)."
        )

    size = sync.subframe_size
    tail = words[sync.offset :]
    n_sub = tail.size // size
    grid = tail[: n_sub * size].reshape(n_sub, size)

    expected = np.array(
        [SYNC_WORDS[i % SUBFRAMES_PER_FRAME] for i in range(n_sub)], dtype=np.uint16
    )
    valid_rows = grid[:, 0] == expected

    # Whole frames only. A trailing partial frame is dropped rather than
    # zero-padded: a padded frame decodes to values that look real.
    n_frames = n_sub // SUBFRAMES_PER_FRAME
    keep = n_frames * SUBFRAMES_PER_FRAME
    data = grid[:keep].reshape(n_frames, SUBFRAMES_PER_FRAME, size)
    valid = valid_rows[:keep].reshape(n_frames, SUBFRAMES_PER_FRAME)

    sfc = _counter(data, layout, "sfc", default_len=4)
    dfc = _counter(data, layout, "dfc", default_len=12)
    return FrameSet(data=data, valid=valid, sync=sync, sfc=sfc, dfc=dfc)


def _counter(data: np.ndarray, layout, which: str, default_len: int) -> np.ndarray:
    subframe = getattr(layout, f"{which}_subframe", 1)
    word = getattr(layout, f"{which}_word", 1)
    lsb = getattr(layout, f"{which}_bit_lsb", 1)
    length = getattr(layout, f"{which}_bit_length", default_len)
    if not (1 <= subframe <= 4) or not (1 <= word <= data.shape[2]):
        return np.zeros(data.shape[0], dtype=np.int64)
    raw = data[:, subframe - 1, word - 1].astype(np.int64)
    return (raw >> (lsb - 1)) & ((1 << length) - 1)
