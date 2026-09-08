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

    def __post_init__(self) -> None:
        if self.words.dtype != np.uint16:
            self.words = self.words.astype(np.uint16)

    @property
    def n_words(self) -> int:
        return int(self.words.size)


class UnsupportedContainer(Exception):
    """The bytes are a recognised format we cannot turn into 717 words."""
