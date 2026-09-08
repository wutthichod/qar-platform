"""The frame grid: a word stream reshaped into addressable time.

One object, built once per file, that everything downstream indexes into.
Separating it from parameter extraction is what lets a decode report "sync
was fine, the FAP is wrong" instead of one undifferentiated failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from qar_decode.arinc717 import SYNC_WORDS, SyncResult, find_sync

__all__ = ["FrameSet", "build"]


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
    def subframe_words(self) -> int:
        return int(self.data.shape[2])

    @property
    def integrity(self) -> float:
        """Fraction of subframes whose sync word was where it should be."""
        return float(self.valid.mean()) if self.valid.size else 0.0

    def word(self, subframe: int, word: int) -> np.ndarray:
        """One word position across every frame. 1-based, as the FAP writes it."""
        return self.data[:, subframe - 1, word - 1]


def build(words: np.ndarray, layout=None, sync: SyncResult | None = None) -> FrameSet:
    """Reshape a word stream into frames and read its counters.

    When a FrameLayout is given, its subframe size is tried first and its
    counter positions are used. The layout is a strong hint, not an
    instruction: if the FAP says 1024 words and the file syncs at 512, the
    file wins and the caller can see the disagreement in ``sync``.
    """
    sizes = (64, 128, 256, 512, 1024, 2048, 4160)
    if layout is not None:
        sizes = (layout.subframe_words,) + tuple(
            s for s in sizes if s != layout.subframe_words
        )

    if sync is None:
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

    expected = np.array([SYNC_WORDS[i % 4] for i in range(n_sub)], dtype=np.uint16)
    valid_rows = grid[:, 0] == expected

    # Whole frames only. A trailing partial frame is dropped rather than
    # zero-padded: a padded frame decodes to values that look real.
    n_frames = n_sub // 4
    data = grid[: n_frames * 4].reshape(n_frames, 4, size)
    valid = valid_rows[: n_frames * 4].reshape(n_frames, 4)

    sfc = _counter(data, layout, "sfc", default_len=4)
    dfc = _counter(data, layout, "dfc", default_len=12)
    return FrameSet(data=data, valid=valid, sync=sync, sfc=sfc, dfc=dfc)


def _counter(data: np.ndarray, layout, which: str, default_len: int) -> np.ndarray:
    if layout is None:
        return np.zeros(data.shape[0], dtype=np.int64)
    subframe = getattr(layout, f"{which}_subframe", 1)
    word = getattr(layout, f"{which}_word", 1)
    lsb = getattr(layout, f"{which}_bit_lsb", 1)
    length = getattr(layout, f"{which}_bit_length", default_len)
    if not (1 <= subframe <= 4) or not (1 <= word <= data.shape[2]):
        return np.zeros(data.shape[0], dtype=np.int64)
    raw = data[:, subframe - 1, word - 1].astype(np.int64)
    return (raw >> (lsb - 1)) & ((1 << length) - 1)
