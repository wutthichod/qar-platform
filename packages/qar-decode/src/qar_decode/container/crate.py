"""Boeing 787 EDS crate and the CPL log inside it.

The outer layer is a Boeing "crate": a zip holding ``crate.xml`` -- an
XML-DSig manifest naming the payload and carrying its SHA-256 -- next to the
payload itself. The digest is checked when present, because a truncated
transfer and a decoder bug look identical from the far end of a pipeline.

The payload is a CPL (Continuous Parameter Logging) stream: records framed
as

    EB 90 | length u16 | sequence u32 | type u16 | payload

with ``length`` covering the whole record. Records arrive interleaved by
rate, and the census across a full flight is what gives them away:

    type 3   one per second        4201-byte payload
    type 4   five per second        237-byte payload
    type 5   ten per second          11-byte payload
    type 7   occasional             flight-event records
    type 8   rare                   flight identity (tail, route, flight no)

So the file already carries the 1 / 5 / 10 Hz structure that the Silver
schema has to represent, before any parameter is extracted.

**What this module does not do.** CPL payloads are not ARINC 717 -- there is
no subframe sync anywhere in them, at any packing, and we checked. Turning
type 3/4/5 payloads into parameters needs Boeing's CPL map, identified in
the type 1 header record (here ``647A-00 CPL_MAP_RR01AA-906``), which is not
in the FAP set. Until that map is available this unwrapper returns the
demuxed records and the flight identity, and ``unwrap`` raises
UnsupportedContainer rather than inventing a word stream that would decode
to convincing nonsense.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from qar_decode.container.base import Recording, UnsupportedContainer

RECORD_SYNC = b"\xeb\x90"
HEADER_LEN = 10


@dataclass
class CplRecord:
    offset: int
    sequence: int
    type: int
    payload: bytes


@dataclass
class CplStream:
    """A demuxed CPL log."""

    records_by_type: dict[int, list[CplRecord]] = field(default_factory=dict)
    census: Counter = field(default_factory=Counter)
    resyncs: int = 0
    identity: dict[str, Any] = field(default_factory=dict)
    map_id: str | None = None

    @property
    def seconds(self) -> int:
        """Recording length, from the one-per-second record type."""
        return len(self.records_by_type.get(3, ()))


def looks_like_crate(data: bytes) -> bool:
    if data[:2] == b"PK":
        return True
    return data[:2] == RECORD_SYNC


def open_crate(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Return (payload, metadata) for a zipped EDS crate."""
    meta: dict[str, Any] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        manifest_name = next((n for n in names if n.endswith("crate.xml")), None)
        digests: dict[str, str] = {}
        if manifest_name:
            xml = zf.read(manifest_name).decode("utf-8", errors="replace")
            meta.update(_parse_crate_xml(xml))
            digests = _manifest_digests(xml)

        payload_name = next(
            (n for n in names if not n.endswith("crate.xml") and not n.endswith("/")),
            None,
        )
        if payload_name is None:
            raise ValueError("crate: no payload member")
        payload = zf.read(payload_name)
        meta["payload_member"] = payload_name

        want = digests.get(payload_name.rsplit("/", 1)[-1])
        if want:
            got = base64.b64encode(hashlib.sha256(payload).digest()).decode()
            meta["digest_verified"] = got == want
            if got != want:
                raise ValueError(
                    f"crate: SHA-256 mismatch for {payload_name}; "
                    "the payload is not what the manifest signed"
                )
    return payload, meta


def _parse_crate_xml(xml: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tag, key in (
        ("Description", "description"),
        ("ShipTo", "ship_to"),
        ("ShipFrom", "ship_from"),
        ("ShipDate", "shipped_at"),
    ):
        m = re.search(rf"<crate:{tag}>(.*?)</crate:{tag}>", xml, re.S)
        if m:
            out[key] = m.group(1).strip()
    # 220806-032650-THA643-RJAA-VTBS-TG-HS-TWB-CPL
    desc = out.get("description", "")
    m = re.match(
        r"(\d{6})-(\d{6})-([A-Z0-9]+)-([A-Z]{4})-([A-Z]{4})-\w+-([A-Z]{2}-[A-Z]{3})", desc
    )
    if m:
        out |= {
            "flight_number": m.group(3),
            "origin": m.group(4),
            "destination": m.group(5),
            "tail_number": m.group(6),
        }
    return out


def _manifest_digests(xml: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(
        r'<ds:Reference URI="([^"#][^"]*)">.*?<ds:DigestValue>(.*?)</ds:DigestValue>',
        xml,
        re.S,
    ):
        out[m.group(1).rsplit("/", 1)[-1]] = m.group(2).strip()
    return out


def demux(payload: bytes, keep: tuple[int, ...] = (1, 7, 8)) -> CplStream:
    """Walk the EB90 record stream.

    ``keep`` names the record types whose payloads are retained in full;
    everything else is counted and indexed but not copied, because the bulk
    types run to 95 MB on a long sector.

    A record whose length is impossible does not end the walk -- the reader
    scans forward to the next EB90 and carries on, counting the resync. Real
    downlinked files have exactly this kind of damage in them, and a decoder
    that stops at the first one throws away the other five hours.
    """
    stream = CplStream()
    offset = 0
    size = len(payload)

    while offset + HEADER_LEN <= size:
        if payload[offset : offset + 2] != RECORD_SYNC:
            nxt = payload.find(RECORD_SYNC, offset + 1)
            if nxt < 0:
                break
            stream.resyncs += 1
            offset = nxt
            continue

        length, sequence, rtype = struct.unpack_from(">HIH", payload, offset + 2)
        if length < HEADER_LEN or offset + length > size:
            nxt = payload.find(RECORD_SYNC, offset + 2)
            if nxt < 0:
                break
            stream.resyncs += 1
            offset = nxt
            continue

        stream.census[rtype] += 1
        body = payload[offset + HEADER_LEN : offset + length] if rtype in keep else b""
        stream.records_by_type.setdefault(rtype, []).append(
            CplRecord(offset=offset, sequence=sequence, type=rtype, payload=body)
        )
        offset += length

    _identify(stream)
    return stream


def _identify(stream: CplStream) -> None:
    for rec in stream.records_by_type.get(1, ())[:1]:
        text = rec.payload.decode("ascii", errors="replace")
        stream.map_id = " ".join(text[:40].split()) or None
        m = re.search(r"([A-Z]{2}-[A-Z]{3})", text)
        if m:
            stream.identity["tail_number"] = m.group(1)

    for rec in stream.records_by_type.get(8, ())[:1]:
        text = rec.payload.decode("ascii", errors="replace")
        m = re.match(r"\s*([A-Z]{2}-[A-Z]{3})([A-Z]{4})([A-Z]{4})([A-Z0-9]+)", text)
        if m:
            stream.identity |= {
                "tail_number": m.group(1),
                "origin": m.group(2),
                "destination": m.group(3),
                "flight_number": m.group(4).strip(),
            }


def inspect(data: bytes) -> tuple[CplStream, dict[str, Any]]:
    """Unwrap and demux without requiring a 717 word stream."""
    if data[:2] == b"PK":
        payload, meta = open_crate(data)
    else:
        payload, meta = data, {}
    stream = demux(payload)

    # Two independent claims about which aircraft this is: the shipping
    # label on the crate, and the recorder's own header inside it. They
    # agree on every real file seen so far. When they do not, the recorder
    # wins -- it was there -- but the disagreement is recorded, because a
    # mislabelled crate means some other flight is mislabelled too.
    conflicts = {
        key: f"crate={meta[key]!r} payload={value!r}"
        for key, value in stream.identity.items()
        if key in meta and meta[key] != value
    }
    meta |= stream.identity
    if conflicts:
        meta["identity_conflict"] = conflicts
    if stream.map_id:
        meta["cpl_map"] = stream.map_id
    return stream, meta


def unwrap(data: bytes) -> Recording:
    stream, meta = inspect(data)
    census = ", ".join(f"type {t}: {n}" for t, n in sorted(stream.census.items()))
    raise UnsupportedContainer(
        "787 CPL demuxed but not decodable to ARINC 717 words "
        f"({stream.seconds} s; {census}; map {stream.map_id!r}). "
        "CPL payloads carry no subframe sync at any packing; extracting "
        "parameters needs Boeing's CPL map, which is not in the FAP set."
    )
