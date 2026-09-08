"""Frame layout documents.

Parses a Safran AGS FAP -- a .lframe plus a directory of .lacquired
parameter documents -- into the objects arinc717 consumes: which subframe,
which word, which bits, and how to convert counts to engineering units.
"""

from qar_decode.fap.ags import (
    AcquiredParameter,
    BitPart,
    Conversion,
    Fap,
    FrameLayout,
    Sample,
    load_fap,
)

__all__ = [
    "AcquiredParameter",
    "BitPart",
    "Conversion",
    "Fap",
    "FrameLayout",
    "Sample",
    "load_fap",
]
