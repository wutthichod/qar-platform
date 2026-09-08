"""ARINC 717 decoding.

This package is deliberately free of cloud dependencies. It takes bytes and
returns parameter values, which means it runs on a laptop, in CI, and in a
container without change. tests/test_import_boundary.py enforces that.

The pipeline, and where each stage lives:

    container/   strip the recorder's wrapper      -> word stream
    arinc717     bytes and bit fields              -> primitives
    frames       locate subframes                  -> frame grid
    fap/         read the frame layout document    -> parameters
    parameters   apply a FAP to a frame grid       -> engineering values
    segment      find flights inside a recording   -> segments
    decode       all of the above, with a report
    arrow        decoded values as Arrow tables    (needs pyarrow)
"""

from qar_decode.arinc717 import (
    SYNC_WORDS,
    Parameter,
    SyncResult,
    WordPart,
    build_frames,
    decode_parameter,
    find_sync,
    superframe_counter,
    unpack_packed,
    unpack_padded,
)
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
    "Parameter",
    "Series",
    "SyncResult",
    "WordPart",
    "build_frames",
    "decode_bytes",
    "decode_file",
    "decode_parameter",
    "find_sync",
    "load_fap",
    "superframe_counter",
    "unpack_packed",
    "unpack_padded",
]
