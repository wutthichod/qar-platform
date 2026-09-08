"""Reading decoded output back.

The value of these tests is the negative cases. A decoded file that is
subtly wrong is still a well-formed Parquet file that opens cleanly in any
tool, so nothing else in the stack will object to it.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from conftest import synth_recording
from qar_decode.arrow import tables
from qar_decode.decode import decode_bytes
from qar_decode.fap import load_fap
from qar_decode.verify import inspect_dir, inspect_file


@pytest.fixture
def written(fap_dir, tmp_path):
    data, _ = synth_recording(n_frames=64)
    decoded = decode_bytes(data, load_fap(fap_dir), source="synthetic")
    out = tmp_path / "silver"
    out.mkdir()
    for rate, table in tables(decoded, load_fap(fap_dir)).items():
        pq.write_table(table, out / f"rate_{rate:02d}hz.parquet", compression="zstd")
    return out


def test_round_trip_recovers_the_decode(written) -> None:
    info = inspect_file(written / "rate_04hz.parquet")
    assert info.parquet_version.startswith("2")
    assert info.compression == "ZSTD"
    assert info.rate == 4
    assert info.n_rows == 64 * 4
    assert info.table_metadata["container"] == "wgl"
    assert info.table_metadata["frame_integrity"] == "1.000000"
    assert not info.problems


def test_column_provenance_survives_the_write(written) -> None:
    """A decoded value whose provenance has been erased cannot be re-checked
    against the FAP, and re-checking is the point of stamping versions."""
    info = inspect_file(written / "rate_04hz.parquet")
    alt = next(c for c in info.columns if c.name == "ALT")
    assert alt.unit == "ft"
    assert alt.bits == 12
    assert alt.rate == 4
    assert alt.native_name == "ALT_LONG"
    assert "w12" in alt.locations


def test_discrete_labels_survive(written) -> None:
    info = inspect_file(written / "rate_04hz.parquet")
    gear = next(c for c in info.columns if c.name == "GEAR")
    assert "On Ground" in gear.labels


def test_signedness_survives(written) -> None:
    info = inspect_file(written / "rate_04hz.parquet")
    assert next(c for c in info.columns if c.name == "PITCH").signed is True
    assert next(c for c in info.columns if c.name == "ALT").signed is False


def test_directory_inspection_orders_by_rate(written) -> None:
    infos = inspect_dir(written)
    assert [i.rate for i in infos] == [1, 4, 8]


def test_dropped_rows_are_caught(written, tmp_path) -> None:
    """Losing rows shifts every timestamp after the fault. The file stays
    perfectly valid Parquet, so only the frames x rate check finds it."""
    table = pq.read_table(written / "rate_04hz.parquet")
    short = table.slice(0, table.num_rows - 10)
    bad = tmp_path / "rate_04hz.parquet"
    pq.write_table(short.replace_schema_metadata(table.schema.metadata), bad)
    problems = inspect_file(bad).problems
    assert any("row count" in p for p in problems)


def test_uneven_timebase_is_caught(written, tmp_path) -> None:
    table = pq.read_table(written / "rate_04hz.parquet")
    t = table.column("t_offset_s").to_numpy().copy()
    t[100:] += 0.5
    columns = [
        pa.array(t) if name == "t_offset_s" else table.column(name)
        for name in table.column_names
    ]
    bad = tmp_path / "rate_04hz.parquet"
    pq.write_table(pa.Table.from_arrays(columns, schema=table.schema), bad)
    assert any("evenly spaced" in p for p in inspect_file(bad).problems)


def test_an_all_null_column_is_caught(written, tmp_path) -> None:
    table = pq.read_table(written / "rate_04hz.parquet")
    columns = [
        pa.array(np.full(table.num_rows, np.nan)) if name == "ALT"
        else table.column(name)
        for name in table.column_names
    ]
    bad = tmp_path / "rate_04hz.parquet"
    pq.write_table(pa.Table.from_arrays(columns, schema=table.schema), bad)
    assert any("ALT: no valid samples" in p for p in inspect_file(bad).problems)


def test_summary_names_the_flight_and_the_versions(written) -> None:
    text = inspect_file(written / "rate_04hz.parquet").summary()
    for expected in ("Apache Parquet", "sample rate", "fap", "decoder",
                     "parameters", "checks"):
        assert expected in text
