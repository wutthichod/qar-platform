"""What every container unwrapper returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Recording:
    """The inner word stream, plus whatever the wrapper knew about it.

    ``words`` is uint16 holding 12-bit values, already masked. Everything
    else the container knew -- tail number, flight number, times -- lives in
    ``metadata`` rather than being thrown away. Recorder metadata is the
    cheapest cross-check there is on a decode that went sideways, and the
    container is the only place it exists.
    """

    words: np.ndarray
    container: str
    word_order: str                       # "big" or "little"
    metadata: dict[str, Any] = field(default_factory=dict)
    # (start_word, length) regions the container flagged as unusual. Their
    # meaning is container-specific; none of them imply missing data.
    blocks: list[tuple[int, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # 12 bits of payload in a 16-bit slot. The pad bits are not ours.
    WORD_MASK = 0x0FFF

    def __post_init__(self) -> None:
        if self.words.dtype != np.uint16:
            self.words = self.words.astype(np.uint16)

        # Establish the 12-bit invariant here rather than trusting every
        # unwrapper to remember it. An unmasked stream does not fail loudly:
        # 0xF247 never equals the 0x247 sync pattern, so sync simply never
        # locks and the decode reports an unreadable file -- which is the
        # wrong conclusion about a perfectly good recording, and the exact
        # misdiagnosis this package exists to avoid.
        #
        # The max() scan costs one pass and no allocation; the copy happens
        # only when there is really something to strip.
        if self.words.size and self.words.max() > self.WORD_MASK:
            self.words = self.words & self.WORD_MASK


class UnsupportedContainer(Exception):
    """The bytes are a recognised format we cannot turn into 717 words."""
