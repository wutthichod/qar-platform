"""ARINC 717 decoding.

This package is deliberately free of cloud dependencies. It takes bytes and
returns parameter values, which means it runs on a laptop, in CI, and in a
container without change. tests/test_import_boundary.py enforces that.

The pipeline, and where each stage lives:

    container/   strip the recorder's wrapper      -> word stream
    arinc717     locate subframes in that stream   -> size and offset
    frames       reshape into addressable time     -> frame grid
    fap/         read the Safran AGS FAP           -> parameters
    parameters   apply a FAP to a frame grid       -> engineering values
    segment      find flights inside a recording   -> segments
    decode       all of the above, with a report
    arrow        decoded values as Arrow tables    (needs pyarrow)
    verify       read a decoded file back          (needs pyarrow)
    selftest     offline end-to-end proof

Only what callers outside the package actually use is re-exported here.
"""

from qar_decode.arinc717 import SYNC_WORDS, SyncResult, find_sync
from qar_decode.decode import (
    DECODER_VERSION,
    Decoded,
    DecodeReport,
    decode_bytes,
    decode_file,
)
from qar_decode.fap import Fap, load_fap
from qar_decode.frames import FrameSet
from qar_decode.parameters import Series

__all__ = [
    "DECODER_VERSION",
    "SYNC_WORDS",
    "Decoded",
    "DecodeReport",
    "Fap",
    "FrameSet",
    "Series",
    "SyncResult",
    "decode_bytes",
    "decode_file",
    "find_sync",
    "load_fap",
]
