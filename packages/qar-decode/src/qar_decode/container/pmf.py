"""Airbus A350 ACMS ``.pmf``.

Three wrappers deep, and each one is a standard format wearing an unusual
extension:

    .pmf  is a tar         -- a manifest XML plus a nested tar
    inner is a tar         -- one gzip member
    member is gzip         -- the ACMS payload

The payload opens with a self-describing header (channel names, a parameter
dictionary, flight identifiers) and then runs ARINC 717 words to the end of
the file. The header is not a fixed size, so we do not guess at one: sync
search finds where the frames start, which is the same operation we need
anyway.

The words are 12 bits in **big-endian** 16-bit slots. That is the detail
that costs an afternoon -- the B777 recorder in the same fleet writes
little-endian, so a decoder that hardcodes one silently produces garbage for
the other rather than failing.
"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
from typing import Any

import numpy as np

from qar_decode.container.base import Recording

MAGIC_HINT = b"ustar"

# The manifest uses single-letter tags. Names from the ACMS ground spec.
_MANIFEST_TAGS = {
    "a": "manifest_id",
    "b": "report_type",
    "n": "acms_node",
    "r": "report_name",
    "t": "flight_number",
    "f": "recording_id",
    "s": "recorded_at",
    "g": "expires_at",
    "v": "payload_bytes",
}


def looks_like_pmf(data: bytes) -> bool:
    return len(data) > 512 and data[257:262] == MAGIC_HINT


def _parse_manifest(xml: bytes) -> dict[str, Any]:
    text = xml.decode("utf-8", errors="replace")
    out: dict[str, Any] = {}
    for tag, name in _MANIFEST_TAGS.items():
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if m:
            out[name] = m.group(1).strip()
    node = out.get("acms_node", "")
    if "." in node:
        out["tail_number"] = node.split(".", 1)[1]
    return out


def unwrap(data: bytes) -> Recording:
    meta: dict[str, Any] = {}
    notes: list[str] = []

    with tarfile.open(fileobj=io.BytesIO(data)) as outer:
        inner_bytes = None
        for member in outer.getmembers():
            if not member.isfile():
                continue
            if member.name.endswith(".xml"):
                extracted = outer.extractfile(member)
                if extracted is not None:
                    meta.update(_parse_manifest(extracted.read()))
            elif member.name.endswith(".tar"):
                extracted = outer.extractfile(member)
                if extracted is not None:
                    inner_bytes = extracted.read()

    if inner_bytes is None:
        raise ValueError("pmf: no nested tar in the outer archive")

    payload = None
    with tarfile.open(fileobj=io.BytesIO(inner_bytes)) as inner:
        for member in inner.getmembers():
            if not member.isfile():
                continue
            extracted = inner.extractfile(member)
            if extracted is None:
                continue
            raw = extracted.read()
            payload = gzip.decompress(raw) if member.name.endswith(".gz") else raw
            meta["payload_member"] = member.name
            break

    if payload is None:
        raise ValueError("pmf: nested tar held no payload")

    meta.update(_payload_identity(payload))
    notes.append(f"payload {len(payload)} bytes after gunzip")

    usable = len(payload) // 2 * 2
    words = (np.frombuffer(payload[:usable], dtype=">u2") & 0x0FFF).astype(np.uint16)
    return Recording(
        words=words,
        container="pmf",
        word_order="big",
        metadata=meta,
        notes=notes,
    )


def _payload_identity(payload: bytes) -> dict[str, Any]:
    """Pull the few identifiers the ACMS header carries in clear ASCII.

    Best effort by design. These are a cross-check on the manifest, not a
    source of truth, so a header layout change must not fail the decode.
    """
    head = payload[:65536]
    out: dict[str, Any] = {}
    m = re.search(rb"\.([A-Z]{2}-[A-Z]{3})\x00", head)
    if m:
        out["payload_tail_number"] = m.group(1).decode()
    m = re.search(rb"\b([A-Z]{4} [A-Z]{4})\b", head)
    if m:
        origin, destination = m.group(1).decode().split()
        out["origin"] = origin
        out["destination"] = destination
    return out
