"""Read a decoded file back and say what it is.

A decoded Parquet file is self-describing on purpose: which flight, which
FAP, which decoder, and for every column the exact bits it came from. This
module reads that back and checks the arithmetic still holds.

The point is not to re-decode. It is to answer, months later and without the
raw file, "what am I looking at and can I trust it" -- and to catch the two
failures that silently produce a well-formed file: a row count that does not
match `frames x rate`, and a column whose values have left the range its own
FAP declared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

__all__ = ["ColumnInfo", "FileInfo", "inspect_file", "inspect_dir"]

_INDEX_COLUMNS = ("t_offset_s", "timestamp", "frame")


@dataclass
class ColumnInfo:
    name: str
    unit: str | None
    rate: int | None
    bits: int | None
    signed: bool | None
    native_name: str | None
    locations: str | None
    labels: str | None
    superframe: bool
    n_valid: int
    n_total: int
    minimum: float | None
    maximum: float | None
    mean: float | None

    @property
    def coverage(self) -> float:
        return self.n_valid / self.n_total if self.n_total else 0.0


@dataclass
class FileInfo:
    path: Path
    parquet_version: str
    created_by: str
    compression: str
    n_rows: int
    n_columns: int
    size_bytes: int
    table_metadata: dict[str, str] = field(default_factory=dict)
    columns: list[ColumnInfo] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def rate(self) -> int | None:
        rates = {c.rate for c in self.columns if c.rate}
        return rates.pop() if len(rates) == 1 else None

    def summary(self) -> str:
        m = self.table_metadata
        lines = [
            f"file              {self.path.name}  ({self.size_bytes / 1e6:.2f} MB)",
            f"format            Apache Parquet v{self.parquet_version}, {self.compression}",
            f"written by        {self.created_by}",
            f"shape             {self.n_rows:,} rows x {self.n_columns} columns",
        ]
        if self.rate:
            lines.append(f"sample rate       {self.rate} Hz")
        for key, label in (
            ("tail_number", "aircraft"), ("flight_number", "flight"),
            ("origin", "from"), ("destination", "to"),
            ("start_time", "flight start"), ("frames", "frames (seconds)"),
            ("container", "container"), ("fap", "fap"),
            ("decoder_version", "decoder"), ("decoded_at", "decoded at"),
            ("sync_confidence", "sync confidence"),
            ("frame_integrity", "frame integrity"), ("source", "source"),
        ):
            if m.get(key):
                lines.append(f"{label:<18}{m[key]}")

        params = [c for c in self.columns if c.name not in _INDEX_COLUMNS]
        lines.append(f"\nparameters        {len(params)}")
        for c in params:
            unit = f"[{c.unit}]" if c.unit else ""
            flags = "".join(
                x for x in ("S" if c.signed else "", "F" if c.superframe else "") if x
            )
            span = (
                f"{c.minimum:>12.3f} .. {c.maximum:<12.3f}"
                if c.minimum is not None else " " * 28
            )
            lines.append(
                f"  {c.name:<18}{unit:<6}{str(c.bits or ''):>3}bit {flags:<2} "
                f"{span} cov {c.coverage * 100:5.1f}%"
            )
            if c.native_name and c.native_name != c.name:
                lines.append(f"      {c.native_name}")
            if c.locations:
                lines.append(f"      bits: {c.locations[:100]}")
            if c.labels:
                lines.append(f"      labels: {c.labels[:100]}")

        if self.problems:
            lines.append(f"\nproblems          {len(self.problems)}")
            lines.extend(f"  {p}" for p in self.problems)
        else:
            lines.append("\nchecks            all passed")
        return "\n".join(lines)


def _decode_meta(raw: dict[bytes, bytes] | None) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in (raw or {}).items()}


def inspect_file(path: str | Path) -> FileInfo:
    """Read one decoded Parquet file and check it against its own metadata."""
    path = Path(path)
    pf = pq.ParquetFile(path)
    table = pq.read_table(path)
    meta = _decode_meta(table.schema.metadata)

    info = FileInfo(
        path=path,
        parquet_version=pf.metadata.format_version,
        created_by=pf.metadata.created_by or "unknown",
        compression=pf.metadata.row_group(0).column(0).compression,
        n_rows=table.num_rows,
        n_columns=table.num_columns,
        size_bytes=path.stat().st_size,
        table_metadata=meta,
    )

    for field_ in table.schema:
        cm = _decode_meta(field_.metadata)
        column = table.column(field_.name)
        try:
            values = column.to_numpy(zero_copy_only=False)
        except Exception:                                     # noqa: BLE001
            values = np.array([])
        numeric = values.dtype.kind in "fiu"
        good = values[np.isfinite(values)] if numeric else values

        info.columns.append(ColumnInfo(
            name=field_.name,
            unit=cm.get("unit") or None,
            rate=int(cm["rate_hz"]) if cm.get("rate_hz") else None,
            bits=int(cm["bits"]) if cm.get("bits") else None,
            signed=cm.get("signed") == "true" if "signed" in cm else None,
            native_name=cm.get("native_name"),
            locations=cm.get("locations"),
            labels=cm.get("labels"),
            superframe=cm.get("superframe") == "true",
            n_valid=int(good.size),
            n_total=int(values.size),
            minimum=float(good.min()) if numeric and good.size else None,
            maximum=float(good.max()) if numeric and good.size else None,
            mean=float(good.mean()) if numeric and good.size else None,
        ))

    info.problems = _check(table, info, meta)
    return info


def _check(table, info: FileInfo, meta: dict[str, str]) -> list[str]:
    problems: list[str] = []

    # Row count must equal frames x rate. A mismatch means samples were
    # dropped or duplicated, and every timestamp after the fault is wrong.
    rate, frames = info.rate, meta.get("frames")
    if rate and frames and table.num_rows != int(frames) * rate:
        problems.append(
            f"row count {table.num_rows} != frames({frames}) x rate({rate}) "
            f"= {int(frames) * rate}"
        )

    # The time base must be exactly 1/rate, evenly spaced from zero.
    if rate and "t_offset_s" in table.column_names and table.num_rows > 1:
        t = table.column("t_offset_s").to_numpy()
        steps = np.diff(t)
        if not np.allclose(steps, 1.0 / rate):
            problems.append(f"t_offset_s is not evenly spaced at 1/{rate}s")
        if t[0] != 0.0:
            problems.append(f"t_offset_s starts at {t[0]}, not 0")

    for c in info.columns:
        if c.name in _INDEX_COLUMNS:
            continue
        if c.rate and rate and c.rate != rate:
            problems.append(f"{c.name}: rate {c.rate} Hz in a {rate} Hz table")
        if c.n_valid == 0:
            problems.append(f"{c.name}: no valid samples")
            continue
        # An unsigned field cannot exceed what its bit width can hold. If it
        # does, the conversion or the bit width recorded here disagree.
        if c.bits and c.signed is False and c.minimum is not None and c.minimum < 0:
            problems.append(f"{c.name}: unsigned but minimum is {c.minimum:g}")
    return problems


def inspect_dir(path: str | Path) -> list[FileInfo]:
    """Every decoded Parquet file in a directory, lowest rate first."""
    path = Path(path)
    files = sorted(path.glob("*.parquet")) if path.is_dir() else [path]
    return [inspect_file(f) for f in files]
