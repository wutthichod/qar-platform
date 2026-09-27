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
    probe: int = 64,
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

    Each candidate is rejected on a short ``probe`` of strides before
    anything expensive happens, then the survivor is scored across the whole
    recording, so the returned confidence describes the file rather than its
    first few seconds. Real recordings drop subframes, so a perfect run is
    not required -- demanding one is a common cause of false 'unreadable
    file' verdicts.
    """
    best: SyncResult | None = None
    total_words = words.size

    # Every position the first sync pattern occupies, anywhere in the file.
    # Computed once: it does not depend on the subframe size, and scanning
    # the whole recording rather than a window near the front is what makes
    # this robust to a header of any length. Measured at 1 ms on a 22
    # million word file -- cheap enough that bounding the search would be
    # trading a real failure mode for nothing.
    starts = np.flatnonzero(words == sync_words[0])
    if starts.size == 0:
        return None
    starts = starts[:max_candidates]

    for subframe_words in candidate_sizes:
        for offset in starts:
            offset = int(offset)

            # How many whole subframes follow this offset. Computed rather
            # than materialised: building the full stride array for every
            # candidate is what makes a file that never locks expensive,
            # and on a 22-million-word recording most candidates are noise.
            n_positions = (
                total_words - subframe_words - offset
            ) // subframe_words + 1

            # A candidate needs a whole frame to be judged on: all four
            # sync patterns must be seen in sequence, or one stray 0x247 in
            # a short file looks like a lock. This also covers a recording
            # shorter than a single frame at this size.
            if n_positions < SUBFRAMES_PER_FRAME:
                continue

            # Cheap rejection first. A wrong phase matches at roughly one
            # in 4096, so a couple of dozen strides separate signal from
            # noise past any doubt, and nearly every candidate dies here for
            # the cost of a few dozen comparisons rather than a few
            # thousand.
            n_probe = min(probe, n_positions)
            probe_at = offset + subframe_words * np.arange(n_probe)
            expected = np.array(
                [sync_words[i % len(sync_words)] for i in range(n_probe)],
                dtype=np.uint16,
            )
            if np.count_nonzero(words[probe_at] == expected) / n_probe < min_confidence:
                continue

            # Survivor: score it across the whole recording, so the returned
            # confidence describes the file and not its first few seconds.
            positions = offset + subframe_words * np.arange(n_positions)
            expected_full = np.array(
                [sync_words[i % len(sync_words)] for i in range(n_positions)],
                dtype=np.uint16,
            )
            confidence = (
                np.count_nonzero(words[positions] == expected_full) / n_positions
            )
            if confidence < min_confidence:
                continue

            result = SyncResult(
                subframe_size=subframe_words, offset=offset, confidence=confidence
            )
            if best is None or result.confidence > best.confidence:
                best = result
            break

        # Sizes are tried in the caller's order; a clean lock ends the search.
        if best is not None and best.confidence >= 0.999:
            break

    return best
