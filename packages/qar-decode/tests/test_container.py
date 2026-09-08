"""Container unwrapping, including the parts that are easy to get backwards."""

from __future__ import annotations

import gzip
import io
import struct
import tarfile
import zipfile

import numpy as np
import pytest

from qar_decode.container import UnsupportedContainer, crate, detect, pmf, unwrap, wgl
from conftest import synth_recording


def _make_pmf(payload: bytes) -> bytes:
    def tar_of(members: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, body in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tf.addfile(info, io.BytesIO(body))
        return buf.getvalue()

    manifest = (
        b'<?xml version="1.0"?><MH><a>ID</a><n>ACMS.HS-TST</n>'
        b"<t>TG0001</t><s>2022-03-27T12:15:30</s></MH>"
    )
    inner = tar_of({"REC.gz": gzip.compress(payload)})
    return tar_of({"ID.xml": manifest, "ID.tar": inner})


def test_pmf_is_tar_tar_gzip_and_big_endian() -> None:
    payload, _ = synth_recording(n_frames=8, big_endian=True)
    rec = unwrap(_make_pmf(payload))
    assert rec.container == "pmf"
    assert rec.word_order == "big"
    assert rec.metadata["tail_number"] == "HS-TST"
    assert rec.metadata["flight_number"] == "TG0001"
    assert rec.words[0] == 0x247


def test_wgl_is_little_endian() -> None:
    payload, _ = synth_recording(n_frames=8, big_endian=False)
    rec = unwrap(payload)
    assert rec.container == "wgl"
    assert rec.word_order == "little"
    assert rec.words[0] == 0x247


def test_word_order_confusion_is_not_silent() -> None:
    """A big-endian file read little-endian must not look like valid words.

    The two recorders in this fleet disagree on byte order, so this is the
    mistake most likely to be made and least likely to announce itself."""
    payload, _ = synth_recording(n_frames=8, big_endian=True)
    wrong = np.frombuffer(payload, dtype="<u2") & 0x0FFF
    assert wrong[0] != 0x247


def test_wgl_set_pad_nibble_is_data_not_a_gap() -> None:
    """Runs with the pad bits set carry real words; masking is all they need."""
    payload, _ = synth_recording(n_frames=8, pad_nibble=0xF)
    rec = wgl.unwrap(payload)
    assert rec.words[0] == 0x247
    assert rec.blocks, "the padded region should still be reported"
    assert "data retained" in rec.notes[0]


def _make_crate() -> bytes:
    records = [
        (1, b"647A-00 CPL_MAP_TEST HS-TST    "),
        (3, b"\x00" * 64),
        (4, b"\x11" * 32),
        (5, b"\x22" * 8),
    ]
    payload = b""
    for seq, (rtype, body) in enumerate(records):
        payload += b"\xeb\x90" + struct.pack(">HIH", len(body) + 10, seq, rtype) + body

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "crate.xml",
            "<crate:EDSCrate xmlns:crate='x'><crate:CrateData><crate:ShippingLabel>"
            "<crate:Description>220806-032650-THA643-RJAA-VTBS-TG-HS-TWB-CPL"
            "</crate:Description></crate:ShippingLabel></crate:CrateData></crate:EDSCrate>",
        )
        zf.writestr("payload", payload)
    return buf.getvalue()


def test_crate_demuxes_records_and_reads_identity() -> None:
    stream, meta = crate.inspect(_make_crate())
    assert meta["origin"] == "RJAA"
    assert meta["destination"] == "VTBS"
    assert meta["cpl_map"].startswith("647A-00")
    assert dict(stream.census) == {1: 1, 3: 1, 4: 1, 5: 1}
    assert stream.resyncs == 0


def test_crate_records_a_label_that_disagrees_with_the_recorder() -> None:
    """The label says HS-TWB, the recorder header says HS-TST. The recorder
    wins, but a silent overwrite would hide a mislabelled crate."""
    _, meta = crate.inspect(_make_crate())
    assert meta["tail_number"] == "HS-TST"
    assert "tail_number" in meta["identity_conflict"]


def test_crate_resyncs_past_damage_instead_of_stopping() -> None:
    """Downlinked files have damage in them. Stopping at the first bad
    record throws away every good one after it."""
    good = _make_crate()
    payload, _ = crate.open_crate(good)
    damaged = payload[:12] + b"\xff\xff\xff\xff" + payload[12:]
    stream = crate.demux(damaged)
    assert stream.resyncs >= 1
    assert sum(stream.census.values()) >= 3


def test_crate_refuses_a_payload_the_manifest_did_not_sign() -> None:
    raw = _make_crate()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(buf, "w") as dst:
        dst.writestr(
            "crate.xml",
            '<x xmlns:ds="y"><ds:Reference URI="payload">'
            "<ds:DigestValue>AAAA</ds:DigestValue></ds:Reference></x>",
        )
        dst.writestr("payload", src.read("payload"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        crate.open_crate(buf.getvalue())


def test_cpl_is_reported_as_unsupported_not_decoded_as_garbage() -> None:
    with pytest.raises(UnsupportedContainer, match="CPL"):
        crate.unwrap(_make_crate())


def test_detect_reads_bytes_not_filenames() -> None:
    payload, _ = synth_recording(n_frames=8)
    assert detect(payload) == "wgl"
    assert detect(_make_pmf(payload)) == "pmf"
    assert detect(_make_crate()) == "crate"
    with pytest.raises(UnsupportedContainer):
        detect(b"not a recording")
