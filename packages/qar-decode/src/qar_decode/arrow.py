"""Decoded parameters as Arrow tables.

Kept out of the core so qar_decode itself stays numpy-only; import this
module only where pyarrow is actually installed.

One table per native rate. The alternative -- a single wide table at the
maximum rate -- costs a factor of 32 on the 1 Hz parameters and invents
samples that were never recorded, which then have to be un-invented by
anyone computing statistics.

Native names, units, bit widths and word positions travel as column
metadata. A decoded value whose provenance has been erased cannot be
re-checked against the FAP, and re-checking is the whole point of stamping
versions on every row.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from qar_decode.decode import Decoded, timestamps
from qar_decode.frames import FRAME_SECONDS

__all__ = ["tables", "report_table"]


def _field(name: str, series, param_meta: dict[str, str]) -> pa.Field:
    return pa.field(name, pa.float64(), nullable=True, metadata=param_meta)


def rate_label(rate_hz: float) -> str:
    """Filename-safe name for a rate. 0.25 Hz is a real rate: one sample per
    four-second frame, which is what a great many FAP entries are."""
    return f"{rate_hz:g}".replace(".", "p")


def tables(decoded: Decoded, fap=None) -> dict[float, pa.Table]:
    """One Arrow table per native sample rate, keyed by hertz."""
    out: dict[float, pa.Table] = {}
    meta = decoded.report

    for rate_hz, names in decoded.by_rate().items():
        per_frame = int(round(rate_hz * FRAME_SECONDS))
        columns: list[pa.Array] = []
        fields: list[pa.Field] = []

        offsets = decoded.timebase(rate_hz)
        fields.append(pa.field("t_offset_s", pa.float64(), nullable=False))
        columns.append(pa.array(offsets))

        stamps = timestamps(decoded, rate_hz)
        if stamps is not None:
            fields.append(pa.field("timestamp", pa.timestamp("ns", tz="UTC")))
            columns.append(pa.array(stamps))

        fields.append(pa.field("frame", pa.int32(), nullable=False))
        columns.append(
            pa.array(np.repeat(np.arange(meta.n_frames, dtype=np.int32), per_frame))
        )

        for name in names:
            s = decoded.series[name]
            param = fap.parameters.get(name) if fap is not None else None
            column_meta = {
                "native_name": param.name if param else name,
                "mnemonic": name,
                "unit": s.unit or "",
                "rate_hz": f"{s.rate_hz:g}",
                "samples_per_frame": str(s.samples_per_frame),
                "bits": str(s.bits),
                "signed": str(s.signed).lower(),
                "encoding": s.encoding,
                "fap": meta.fap,
                "decoder_version": meta.decoder_version,
            }
            if param is not None:
                where = "; ".join(
                    f"sf{sample.subframe}:w{part.word}"
                    f"[{part.src_lsb}:{part.src_lsb + part.length - 1}]"
                    for sample in param.samples[:4]
                    for part in sample.parts
                )
                column_meta["locations"] = where
                if param.superframe:
                    column_meta["superframe"] = "true"
                if param.field_widths:
                    column_meta["digit_widths"] = "".join(map(str, param.field_widths))
            if s.labels:
                column_meta["labels"] = "; ".join(
                    f"{k}={v}" for k, v in sorted(s.labels.items())
                )
            if s.text is not None:
                fields.append(pa.field(name, pa.string(), nullable=True,
                                       metadata=column_meta))
                columns.append(pa.array(s.text, type=pa.string()))
            else:
                fields.append(_field(name, s, column_meta))
                columns.append(pa.array(s.values))

        schema = pa.schema(fields, metadata=_table_metadata(decoded))
        out[rate_hz] = pa.Table.from_arrays(columns, schema=schema)

    return out


def _table_metadata(decoded: Decoded) -> dict[str, str]:
    r = decoded.report
    meta: dict[str, Any] = {
        "source": r.source,
        "container": r.container,
        "fap": r.fap,
        "decoder_version": r.decoder_version,
        "subframe_words": str(r.subframe_words),
        "sync_offset": str(r.sync_offset),
        "sync_confidence": f"{r.sync_confidence:.6f}",
        "frames": str(r.n_frames),
        "frame_seconds": f"{FRAME_SECONDS:g}",
        "duration_s": f"{r.duration_s:g}",
        "frame_integrity": f"{r.frame_integrity:.6f}",
        "decoded_at": r.started_at or "",
    }
    for key in ("tail_number", "flight_number", "origin", "destination",
                "recorded_at", "start_time", "start_time_source",
                "clock_agreement", "clock_max_drift_s", "clock_rate"):
        value = r.container_metadata.get(key)
        if value:
            meta[key] = str(value)
    return {k: v for k, v in meta.items() if v}


def report_table(decoded: Decoded) -> pa.Table:
    """The decode report as a single row, for the registry."""
    r = decoded.report
    return pa.table(
        {
            "source": [r.source],
            "container": [r.container],
            "fap": [r.fap],
            "decoder_version": [r.decoder_version],
            "tail_number": [r.container_metadata.get("tail_number")],
            "flight_number": [r.container_metadata.get("flight_number")],
            "origin": [r.container_metadata.get("origin")],
            "destination": [r.container_metadata.get("destination")],
            "start_time": [r.container_metadata.get("start_time")],
            "start_time_source": [r.container_metadata.get("start_time_source")],
            "subframe_words": [r.subframe_words],
            "sync_offset": [r.sync_offset],
            "sync_confidence": [r.sync_confidence],
            "frames": [r.n_frames],
            "duration_s": [r.duration_s],
            "frame_integrity": [r.frame_integrity],
            "parameters_decoded": [len(r.decoded)],
            "parameters_failed": [len(r.failed)],
            "out_of_range": [len(r.out_of_range)],
            "decoded_at": [r.started_at],
            "elapsed_s": [r.elapsed_s],
        }
    )
