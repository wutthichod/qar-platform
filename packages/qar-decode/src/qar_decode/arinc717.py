"""ARINC 717 subframe location.

A recording is a stream of 12-bit words. Every subframe opens with one of
four sync patterns, cycling in order, spaced one subframe apart. Finding
that pattern gives the two facts everything downstream depends on: how long
a subframe is, and where the first one starts.

That is all this module does. Unpacking bytes into words belongs to
``container``, because word order is a property of the recorder and not of
the standard; turning located frames into values belongs to ``frames`` and
``parameters``, because that needs a FAP.

Sync search failing is a different problem from a FAP being wrong, and
keeping them in separate modules is what makes decode failures diagnosable
rather than mysterious.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

# Subframe synchronisation patterns, 12-bit, in the order they cycle.
SYNC_WORDS: tuple[int, ...] = (0x247, 0x5B8, 0xA47, 0xDB8)

# ARINC 717 frame geometry. Not configurable, and not arbitrary: there are
# four subframes to a frame because there are four sync patterns to cycle
# through, which is why this is derived from SYNC_WORDS rather than written
# out as a 4 in the several places that need it.
SUBFRAMES_PER_FRAME = len(SYNC_WORDS)

# A subframe is one second. That is what turns a word position into a
# sample rate, and it makes a frame four seconds -- not one. Conflating the
# two is a silent fourfold error in every duration, rate and timestamp the
# decoder produces, and it still yields a well-formed file, so nothing
# downstream objects. Verified against recorded UTC: see docs/formats.md.
SUBFRAME_SECONDS = 1.0
FRAME_SECONDS = SUBFRAMES_PER_FRAME * SUBFRAME_SECONDS


@dataclass(frozen=True)
class SyncResult:
    subframe_size: int       # words per subframe
    offset: int              # index of the first subframe-1 sync word
    confidence: float        # fraction of expected syncs actually present


def find_sync(
    words: np.ndarray,
    candidate_sizes: Iterable[int],
    sync_words: tuple[int, ...] = SYNC_WORDS,
    min_confidence: float = 0.5,
    max_candidates: int = 4096,
    probe: int = 4000,
) -> SyncResult | None:
    """Locate the recording's subframe size and phase.

    For each candidate size, take the positions where the *first* sync
    pattern actually occurs and test each as a phase, checking whether the
    rest of the pattern follows at the expected stride.

    Scanning occurrences rather than every offset in ``0..size`` is what
    makes this work on real files. A recording that opens with a header --
    the A350's ACMS payload, the B777's leading unwritten block -- has its
    first subframe boundary thousands of words in, and an offset scan
    bounded by one subframe never reaches it. That failure looks exactly
    like an unreadable file, which is the wrong conclusion to draw about a
    perfectly good recording.

    Only ``sync_words[0]`` is searched, so the returned offset is always a
    subframe-1 boundary. A file that opens mid-frame loses up to three
    subframes from the front rather than needing a phase to be tracked
    through everything downstream.

    Confidence is judged on a probe of up to ``probe`` strides for speed,
    then recomputed across the whole recording for the winner, so the
    returned number describes the file rather than its first few seconds.
    Real recordings drop subframes, so a perfect run is not required --
    demanding one is a common cause of false 'unreadable file' verdicts.
    """
    best: SyncResult | None = None

    for size in candidate_sizes:
        # A size is only testable if the file holds a whole frame at it:
        # all four sync patterns have to be seen to tell a lock from a
        # coincidence. Cheap enough to run before the scan below, and it is
        # the same condition `positions.size < 4` enforces per offset.
        if words.size < size * SUBFRAMES_PER_FRAME:
            continue

        # Search well past the start of the file, for the header reason above.
        window = min(max(size * 64, 1 << 13), words.size)
        starts = np.flatnonzero(words[:window] == sync_words[0])
        if starts.size == 0:
            continue

        for offset in starts[:max_candidates]:
            offset = int(offset)
            # Inclusive upper bound: a subframe starting at exactly
            # words.size - size is whole and is one `frames.build` will
            # construct. Excluding it drops the last subframe from the
            # confidence figure -- so a recording whose final subframe is
            # corrupt reports a clean 1.000000 -- and leaves a one-frame
            # recording with three positions, below the minimum, unlockable.
            positions = np.arange(offset, words.size - size + 1, size)
            if positions.size < SUBFRAMES_PER_FRAME:
                continue

            sample = positions[:probe]
            expected = np.array(
                [sync_words[i % len(sync_words)] for i in range(sample.size)],
                dtype=np.uint16,
            )
            if np.count_nonzero(words[sample] == expected) / sample.size < min_confidence:
                continue

            expected_full = np.array(
                [sync_words[i % len(sync_words)] for i in range(positions.size)],
                dtype=np.uint16,
            )
            confidence = (
                np.count_nonzero(words[positions] == expected_full) / positions.size
            )
            if confidence < min_confidence:
                continue

            result = SyncResult(subframe_size=size, offset=offset, confidence=confidence)
            if best is None or result.confidence > best.confidence:
                best = result
            break

        # Sizes are tried in the caller's order; a clean lock ends the search.
        if best is not None and best.confidence >= 0.999:
            break

    return best
