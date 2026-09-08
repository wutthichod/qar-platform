"""Boeing 777 Teledyne ``.wgl`` / ``raw.dat``.

The plainest of the three: 12-bit words in little-endian 16-bit slots,
running from byte zero with no header to skip.

The one feature worth knowing is the pad nibble. Each 12-bit word sits in a
16-bit slot with four bits left over, and the recorder does not write them
consistently -- most of the file leaves them clear, but a few multi-kiloword
runs have them all set. It is tempting to read those runs as unwritten
flash, and wrong: their contents vary, and every one of them begins on a
subframe boundary with a valid sync word. They are ordinary data whose pad
bits happen to be ones.

So the pad bits are masked off and nothing else is inferred from them. The
regions are still reported, because a file whose pad pattern changes is
worth a second look, but they are not treated as gaps. Whether a subframe
is good is decided by the sync check in ``frames``, which tests the data
rather than the padding around it.
"""

from __future__ import annotations

import numpy as np

from qar_decode.container.base import Recording


def looks_like_wgl(data: bytes) -> bool:
    """True when the file is 12-bit words in 16-bit little-endian slots.

    The test is that the pad nibble is consistent -- either clear or set --
    across the opening words. Random bytes do not do that.
    """
    if len(data) < 4096:
        return False
    pad = np.frombuffer(data[:4096], dtype="<u2") >> 12
    return bool((pad == 0).mean() > 0.9 or (pad == 0xF).mean() > 0.9)


def unwrap(data: bytes) -> Recording:
    usable = len(data) // 2 * 2
    raw = np.frombuffer(data[:usable], dtype="<u2")
    words = (raw & 0x0FFF).astype(np.uint16)

    padded = (raw >> 12) == 0xF
    regions: list[tuple[int, int]] = []
    if padded.any():
        idx = np.flatnonzero(padded)
        cuts = np.flatnonzero(np.diff(idx) != 1)
        starts = np.r_[idx[0], idx[cuts + 1]]
        ends = np.r_[idx[cuts], idx[-1]]
        regions = [(int(s), int(e - s + 1)) for s, e in zip(starts, ends)]

    notes = []
    if regions:
        total = sum(n for _, n in regions)
        notes.append(
            f"{len(regions)} region(s) totalling {total} words carry a set pad "
            "nibble; masked off, data retained"
        )

    return Recording(
        words=words,
        container="wgl",
        word_order="little",
        metadata={},
        blocks=regions,
        notes=notes,
    )
