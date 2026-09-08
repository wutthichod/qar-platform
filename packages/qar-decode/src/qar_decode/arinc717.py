"""ARINC 717 reference decoder.

A readable implementation of the path from raw recorder bytes to engineering
values. Written to be understood and tested rather than to be fast; the
vectorised version comes later, once the semantics are pinned down.

The pipeline is five stages, each independently testable:

    bytes -> 12-bit words -> located subframes -> frame array -> parameters

Stage boundaries matter. Sync search failing is a different problem from a
FAP being wrong, and keeping them separate is what makes decode failures
diagnosable rather than mysterious.

Run `python arinc717.py` to execute a round-trip self-test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

import numpy as np

# Default subframe synchronisation patterns, 12-bit.
# Subframes are named 1..4 because these cycle in this order.
SYNC_WORDS: tuple[int, ...] = (0x247, 0x5B8, 0xA47, 0xDB8)

# Permitted subframe sizes in words per second.
SUBFRAME_SIZES: tuple[int, ...] = (64, 128, 256, 512, 1024)


# ---------------------------------------------------------------------------
# Stage 1 — bytes to 12-bit words
# ---------------------------------------------------------------------------

def unpack_padded(data: bytes, little_endian: bool = True) -> np.ndarray:
    """12-bit words stored one per 16-bit slot, padded with four zero bits.

    The four most significant bits are masked off, because some recorders
    leave rubbish there.
    """
    dtype = "<u2" if little_endian else ">u2"
    words = np.frombuffer(data[: len(data) // 2 * 2], dtype=dtype)
    return (words & 0x0FFF).astype(np.uint16)


def unpack_packed(data: bytes) -> np.ndarray:
    """12-bit words packed three bytes to two words, no padding.

    Byte layout per triple:  AAAAAAAA ABBBBBBB BBBBAAAA  is one common
    variant; this implements the widespread little-endian nibble order
    where word0 = b0 | ((b1 & 0x0F) << 8) and word1 = (b1 >> 4) | (b2 << 4).
    Verify against your own recorder before trusting it.
    """
    usable = len(data) // 3 * 3
    triples = np.frombuffer(data[:usable], dtype=np.uint8).reshape(-1, 3).astype(np.uint16)
    w0 = triples[:, 0] | ((triples[:, 1] & 0x0F) << 8)
    w1 = (triples[:, 1] >> 4) | (triples[:, 2] << 4)
    return np.column_stack([w0, w1]).ravel().astype(np.uint16)


# ---------------------------------------------------------------------------
# Stage 2 — find the sync pattern
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncResult:
    subframe_size: int
    offset: int              # index of the first located sync word
    first_subframe: int      # 1..4, which subframe that sync belongs to
    hits: int                # how many consecutive syncs matched
    confidence: float        # hits / expected


def find_sync(
    words: np.ndarray,
    candidate_sizes: Iterable[int] = SUBFRAME_SIZES,
    sync_words: tuple[int, ...] = SYNC_WORDS,
    min_confidence: float = 0.5,
    max_candidates: int = 4096,
    probe: int = 4000,
) -> SyncResult | None:
    """Locate the recording's subframe size and phase.

    Strategy: for each candidate size, take the positions where the first
    sync pattern actually occurs and test each as a phase, checking whether
    the rest of the pattern follows at the expected stride.

    Scanning occurrences rather than every offset in ``0..size`` is what
    makes this work on real files. A recording that opens with a header --
    the A350's ACMS payload, the B777's unwritten first flash block -- has
    its first subframe boundary thousands of words in, and an offset scan
    bounded by one subframe never reaches it. That failure looks exactly
    like an unreadable file, which is the wrong conclusion to draw about a
    perfectly good recording.

    Confidence is judged on a probe of up to ``probe`` strides for speed,
    then recomputed across the whole recording for the winner, so the
    returned number describes the file rather than its first few seconds.

    Real recordings drop subframes, so a perfect run is not required.
    """
    best: SyncResult | None = None

    for size in candidate_sizes:
        if words.size < size * 4:
            continue

        # Search well past the start of the file. A recording can open with
        # thousands of words that are not frames -- the A350's ACMS payload
        # header, the B777's unwritten first flash block -- and a window
        # sized to one or two subframes never reaches the first real sync.
        window = min(max(size * 64, 1 << 13), words.size)
        starts = np.flatnonzero(words[:window] == sync_words[0])
        if starts.size == 0:
            continue

        for offset in starts[:max_candidates]:
            offset = int(offset)
            positions = np.arange(offset, words.size - size, size)
            if positions.size < 4:
                continue
            sample = positions[:probe]
            expected = np.array(
                [sync_words[i % 4] for i in range(sample.size)], dtype=np.uint16
            )
            if np.count_nonzero(words[sample] == expected) / sample.size < min_confidence:
                continue

            expected_full = np.array(
                [sync_words[i % 4] for i in range(positions.size)], dtype=np.uint16
            )
            hits = int(np.count_nonzero(words[positions] == expected_full))
            confidence = hits / positions.size
            if confidence < min_confidence:
                continue

            result = SyncResult(
                subframe_size=size,
                offset=offset,
                first_subframe=1,
                hits=hits,
                confidence=confidence,
            )
            if best is None or result.confidence > best.confidence:
                best = result
            break

        # A longer subframe that also locks is the real one only if it beats
        # what we have; sizes are tried in the caller's order.
        if best is not None and best.confidence >= 0.999:
            break

    return best


# ---------------------------------------------------------------------------
# Stage 3 — assemble a frame array
# ---------------------------------------------------------------------------

def build_frames(words: np.ndarray, sync: SyncResult) -> tuple[np.ndarray, np.ndarray]:
    """Reshape the word stream into [frame, subframe, word].

    Returns the data array and a boolean validity mask of shape
    [frame, subframe]. Corrupt or missing subframes are left zeroed and
    marked invalid rather than dropped, so timing is preserved -- dropping
    them silently shifts every later sample.
    """
    size = sync.subframe_size
    tail = words[sync.offset:]
    n_subframes = tail.size // size
    if n_subframes == 0:
        return np.zeros((0, 4, size), np.uint16), np.zeros((0, 4), bool)

    grid = tail[: n_subframes * size].reshape(n_subframes, size)

    # Which subframe number does each row claim to be?
    expected_seq = [(sync.first_subframe - 1 + i) % 4 for i in range(n_subframes)]
    valid_rows = np.array(
        [grid[i, 0] == SYNC_WORDS[expected_seq[i]] for i in range(n_subframes)]
    )

    # Pad to a whole number of frames, aligned so subframe 1 starts a frame.
    lead = sync.first_subframe - 1
    total = lead + n_subframes
    n_frames = -(-total // 4)

    data = np.zeros((n_frames * 4, size), np.uint16)
    valid = np.zeros(n_frames * 4, bool)
    data[lead : lead + n_subframes] = grid
    valid[lead : lead + n_subframes] = valid_rows

    return data.reshape(n_frames, 4, size), valid.reshape(n_frames, 4)


def superframe_counter(
    frames: np.ndarray, subframe: int, word: int, modulo: int = 16
) -> np.ndarray:
    """Read the frame counter used to index superframe position.

    `subframe` is 1-based, `word` is the 1-based word position within it.
    """
    return frames[:, subframe - 1, word - 1] % modulo


# ---------------------------------------------------------------------------
# Stage 4 — the FAP: how a parameter maps onto words
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WordPart:
    """One slice of a parameter's raw value.

    subframe: 1..4, or 0 meaning 'present in every subframe'
    word:     1-based position within the subframe
    lsb:      bit position of the least significant bit, 1..12
    length:   number of bits taken
    """
    subframe: int
    word: int
    lsb: int = 1
    length: int = 12


@dataclass(frozen=True)
class Parameter:
    """A FAP entry: where the bits live and what they mean."""
    name: str
    parts: tuple[WordPart, ...]
    scale: float = 1.0
    offset: float = 0.0
    signed: bool = False
    encoding: Literal["binary", "bcd", "discrete"] = "binary"
    units: str | None = None
    # Extra word positions giving a higher sample rate within one subframe.
    extra_words: tuple[int, ...] = field(default_factory=tuple)

    @property
    def total_bits(self) -> int:
        return sum(p.length for p in self.parts)


def _extract_bits(raw: np.ndarray, lsb: int, length: int) -> np.ndarray:
    mask = (1 << length) - 1
    return (raw.astype(np.uint32) >> (lsb - 1)) & mask


def _sign_extend(values: np.ndarray, bits: int) -> np.ndarray:
    sign_bit = 1 << (bits - 1)
    return (values.astype(np.int64) ^ sign_bit) - sign_bit


def _bcd_to_int(values: np.ndarray, bits: int) -> np.ndarray:
    out = np.zeros_like(values, dtype=np.int64)
    digits = bits // 4
    for d in range(digits):
        nibble = (values >> (4 * d)) & 0xF
        out += nibble.astype(np.int64) * (10 ** d)
    return out


def decode_parameter(
    frames: np.ndarray,
    valid: np.ndarray,
    param: Parameter,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (values, valid_mask) for one parameter across all frames.

    Values are float; invalid samples are NaN and also flagged in the mask,
    so downstream code can distinguish 'not recorded' from 'recorded zero'.
    That distinction matters: a zeroed altitude is not the same as a missing
    one, and conflating them is how bad FOQA events get raised.
    """
    subframes = range(1, 5) if param.parts[0].subframe == 0 else (param.parts[0].subframe,)

    samples: list[np.ndarray] = []
    masks: list[np.ndarray] = []

    for sf in subframes:
        word_positions = (param.parts[0].word, *param.extra_words)
        for wp in word_positions:
            combined = np.zeros(frames.shape[0], dtype=np.uint32)
            shift = 0
            # Parts are given most-significant first; build from the end.
            for part in reversed(param.parts):
                word_idx = (wp if part is param.parts[0] else part.word) - 1
                raw = frames[:, sf - 1, word_idx]
                combined |= _extract_bits(raw, part.lsb, part.length) << shift
                shift += part.length

            values = combined.astype(np.int64)
            if param.encoding == "bcd":
                values = _bcd_to_int(combined, param.total_bits)
            elif param.signed:
                values = _sign_extend(combined, param.total_bits)

            samples.append(values.astype(np.float64) * param.scale + param.offset)
            masks.append(valid[:, sf - 1])

    # Interleave so samples appear in recorded time order.
    stacked = np.stack(samples, axis=1).ravel()
    mask = np.stack(masks, axis=1).ravel()
    stacked = np.where(mask, stacked, np.nan)
    return stacked, mask


# ---------------------------------------------------------------------------
# Self-test: synthesise a recording, then decode it back
# ---------------------------------------------------------------------------

def _synthesise(n_frames: int = 20, size: int = 64) -> tuple[bytes, np.ndarray]:
    """Build a fake ARINC 717 recording with a known altitude ramp."""
    rng = np.random.default_rng(717)
    words = np.zeros((n_frames, 4, size), dtype=np.uint16)
    words[:] = rng.integers(0, 4096, size=words.shape, dtype=np.uint16)

    for sf in range(4):
        words[:, sf, 0] = SYNC_WORDS[sf]

    # Altitude: word 12 of every subframe, 12 bits, scale 4 ft.
    # Raw counts are chosen first so the truth is exactly representable;
    # otherwise the test measures quantisation rather than the decoder.
    raw = (np.arange(n_frames * 4, dtype=np.uint16) * 6) % 4096
    truth = raw.astype(np.float64) * 4.0
    words[:, :, 11] = raw.reshape(n_frames, 4)

    # Frame counter in subframe 2, word 5.
    words[:, 1, 4] = np.arange(n_frames, dtype=np.uint16) % 16

    flat = words.ravel()
    padded = flat.astype("<u2").tobytes()
    return padded, truth


def _self_test() -> int:
    print("ARINC 717 decoder self-test")
    data, truth = _synthesise()

    words = unpack_padded(data)
    print(f"  unpacked {words.size} words")

    sync = find_sync(words)
    assert sync is not None, "sync search failed"
    print(
        f"  sync: size={sync.subframe_size} offset={sync.offset} "
        f"first_subframe={sync.first_subframe} confidence={sync.confidence:.2f}"
    )
    assert sync.subframe_size == 64
    assert sync.confidence == 1.0

    frames, valid = build_frames(words, sync)
    print(f"  frames: {frames.shape}, valid subframes: {valid.sum()}/{valid.size}")
    assert frames.shape[0] == 20

    counter = superframe_counter(frames, subframe=2, word=5)
    assert counter[0] == 0 and counter[16] == 0, "superframe counter wrong"
    print(f"  superframe counter cycles correctly over {counter.size} frames")

    altitude = Parameter(
        name="ALTITUDE_STD",
        parts=(WordPart(subframe=0, word=12, lsb=1, length=12),),
        scale=4.0,
        units="ft",
    )
    values, mask = decode_parameter(frames, valid, altitude)
    print(f"  decoded {values.size} altitude samples, {mask.sum()} valid")

    assert values.size == truth.size, f"{values.size} != {truth.size}"
    if not np.allclose(values, truth):
        bad = np.argmax(np.abs(values - truth))
        raise AssertionError(
            f"mismatch at {bad}: got {values[bad]}, expected {truth[bad]}"
        )
    print("  altitude round-trip matches to the sample")

    # Corrupt a subframe and confirm it is flagged, not silently dropped.
    corrupted = words.copy()
    corrupted[64 * 5] = 0x000
    frames2, valid2 = build_frames(corrupted, sync)
    assert valid2.sum() == valid.sum() - 1, "corruption not detected"
    print("  corrupt subframe detected and flagged, timing preserved")

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
