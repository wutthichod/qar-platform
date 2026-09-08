"""Recorder container formats.

A raw QAR file is not bare ARINC 717 words. Each recorder wraps them
differently, and the wrapper is where the flight identity lives:

    .pmf            A350 ACMS     tar -> tar -> gzip -> big-endian words
    .wgl/raw.dat    B777 Teledyne little-endian words, fill blocks, no header
    crate/.zip      B787 EDS      zip -> signed CPL record log (not 717)

``unwrap`` sniffs the bytes and returns a Recording. It does not trust the
file extension: files arrive renamed, and the byte patterns are unambiguous
enough that guessing from a name is a needless way to be wrong.
"""

from __future__ import annotations

from pathlib import Path

from qar_decode.container import crate, pmf, wgl
from qar_decode.container.base import Recording, UnsupportedContainer

__all__ = ["Recording", "UnsupportedContainer", "detect", "unwrap", "unwrap_path"]


def detect(data: bytes) -> str:
    """Name the container format from its bytes."""
    if data[:2] == b"PK" or data[:2] == b"\xeb\x90":
        return "crate"
    if pmf.looks_like_pmf(data):
        return "pmf"
    if wgl.looks_like_wgl(data):
        return "wgl"
    raise UnsupportedContainer(
        f"unrecognised container (first bytes {data[:8].hex()})"
    )


def unwrap(data: bytes) -> Recording:
    kind = detect(data)
    if kind == "pmf":
        return pmf.unwrap(data)
    if kind == "wgl":
        return wgl.unwrap(data)
    if kind == "crate":
        return crate.unwrap(data)
    raise UnsupportedContainer(f"no unwrapper for {kind!r}")


def unwrap_path(path: str | Path) -> Recording:
    return unwrap(Path(path).read_bytes())
