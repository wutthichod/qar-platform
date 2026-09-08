"""Decode job entrypoint: I/O only.

Everything this file does is move bytes and record outcomes. All decoding
lives in qar_decode, which knows nothing about S3, Iceberg or schedulers.

That split is the reason the compute layer is swappable. This file is the
only place that changes if the platform moves off Fargate.

Reads its work from the environment, so it runs identically as an ECS task,
as a Kubernetes Job, or on a laptop:

    SOURCE_BUCKET       bucket holding the raw file            (or LOCAL_SOURCE)
    SOURCE_KEY          key of the raw file
    FAP_URI             s3://bucket/prefix of the FAP dir      (or LOCAL_FAP)
    RAW_ARCHIVE_BUCKET  immutable copy of the raw bytes        (optional)
    LAKEHOUSE_BUCKET    where Silver Parquet lands             (optional)
    LAKEHOUSE_PREFIX    key prefix within it        (default "silver")
    REGISTRY_BUCKET     where registry rows land    (default LAKEHOUSE_BUCKET)
    PARAMETERS          comma-separated mnemonics   (default: the whole FAP)
    AWS_REGION          standard

Exit codes are the contract with the scheduler: 0 decoded, 1 failed, 2 the
job was misconfigured. A retry helps with 1 and never helps with 2.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_FAILED, EXIT_MISCONFIGURED = 0, 1, 2


# ---------------------------------------------------------------------------
# Object store. Imported lazily so the container can run a local decode with
# no credentials and no network, which is what the smoke test does.
# ---------------------------------------------------------------------------

def _s3():
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION"))


def _read_source() -> tuple[bytes, str]:
    local = os.environ.get("LOCAL_SOURCE")
    if local:
        path = Path(local)
        return path.read_bytes(), str(path)

    bucket = os.environ["SOURCE_BUCKET"]
    key = os.environ["SOURCE_KEY"]
    body = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    return body, f"s3://{bucket}/{key}"


def _materialise_fap() -> Path:
    """Put the FAP on local disk.

    A FAP is thousands of small XML files. Downloading them one object at a
    time on every job is slow and pointless, so the expectation is a single
    archive at FAP_URI; a prefix full of loose objects is supported because
    that is how they first arrive from the vendor.
    """
    local = os.environ.get("LOCAL_FAP")
    if local:
        return Path(local)

    uri = os.environ.get("FAP_URI")
    if not uri or not uri.startswith("s3://"):
        raise KeyError("FAP_URI (s3://bucket/prefix) or LOCAL_FAP is required")

    bucket, _, prefix = uri[len("s3://") :].partition("/")
    target = Path(tempfile.mkdtemp(prefix="fap-"))
    client = _s3()

    if prefix.endswith((".zip", ".tar.gz", ".tgz")):
        blob = client.get_object(Bucket=bucket, Key=prefix)["Body"].read()
        _unpack(blob, prefix, target)
        # An archive usually contains one top-level directory; use it.
        entries = [p for p in target.iterdir() if p.is_dir()]
        return entries[0] if len(entries) == 1 else target

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", ()):
            key = obj["Key"]
            rel = key[len(prefix.rstrip("/")) + 1 :]
            if not rel:
                continue
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(dest))
    return target


def _unpack(blob: bytes, name: str, target: Path) -> None:
    if name.endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            zf.extractall(target)
    else:
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            tf.extractall(target)


def _put(bucket: str, key: str, body: bytes, content_type: str) -> str:
    _s3().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    return f"s3://{bucket}/{key}"


# ---------------------------------------------------------------------------
# Registry. Written before the work starts, so a crash leaves evidence.
# ---------------------------------------------------------------------------

def _registry_write(status: str, record: dict[str, Any]) -> None:
    record = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **record,
    }
    bucket = os.environ.get("REGISTRY_BUCKET") or os.environ.get("LAKEHOUSE_BUCKET")
    line = json.dumps(record, default=str)

    # Always on stdout: CloudWatch is the one sink that exists before any
    # bucket does, and a job that cannot write its registry row must still
    # be diagnosable.
    print(f"REGISTRY {line}", flush=True)

    if not bucket:
        return
    key = (
        f"{os.environ.get('REGISTRY_PREFIX', 'registry')}/"
        f"{record.get('run_id', 'unknown')}/{status}.json"
    )
    try:
        _put(bucket, key, line.encode(), "application/json")
    except Exception as exc:                              # noqa: BLE001
        print(f"registry write failed ({type(exc).__name__}: {exc})", file=sys.stderr)


# ---------------------------------------------------------------------------

def main() -> int:
    started = time.time()
    run_id = os.environ.get("RUN_ID") or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )

    try:
        source_uri = os.environ.get("LOCAL_SOURCE") or (
            f"s3://{os.environ['SOURCE_BUCKET']}/{os.environ['SOURCE_KEY']}"
        )
    except KeyError as exc:
        print(f"missing required environment variable: {exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED

    base = {"run_id": run_id, "source": source_uri}
    _registry_write("pending", base)

    try:
        from qar_decode import DECODER_VERSION
        from qar_decode.arrow import report_table, tables
        from qar_decode.decode import decode_bytes
        from qar_decode.fap import load_fap
        from qar_decode.segment import inputs_from_decoded, segment_decoded

        data, source_uri = _read_source()
        base["source"] = source_uri
        base["source_bytes"] = len(data)

        # 1. Archive first. The immutable copy has to exist before anything
        #    can go wrong with the decode, or a bad decode of a file that is
        #    since gone is unreproducible.
        archive = os.environ.get("RAW_ARCHIVE_BUCKET")
        if archive:
            key = f"{os.environ.get('RAW_ARCHIVE_PREFIX', 'raw')}/{run_id}/" + Path(
                source_uri
            ).name
            base["archive"] = _put(archive, key, data, "application/octet-stream")

        # 2. Decode.
        fap_root = _materialise_fap()
        wanted = {p.strip() for p in os.environ.get("PARAMETERS", "").split(",") if p.strip()}
        fap = load_fap(fap_root, only=wanted or None)
        decoded = decode_bytes(data, fap, source=source_uri, only=wanted or None)
        report = decoded.report

        print(report.summary(), flush=True)

        # 3. Segment.
        try:
            segments = segment_decoded(decoded)
            signal = inputs_from_decoded(decoded).source
        except ValueError as exc:
            segments, signal = [], f"unavailable: {exc}"
        print(f"\nsegmentation ({signal}): {len(segments)} segment(s)", flush=True)
        for seg in segments:
            print(f"  {seg.flight_id} seconds {seg.first_second}-{seg.last_second} "
                  f"takeoff={seg.takeoff_second}s touchdown={seg.touchdown_second}s "
                  f"airborne={seg.airborne_s}s", flush=True)

        # 4. Write Silver, one object per native rate.
        written: list[str] = []
        lakehouse = os.environ.get("LAKEHOUSE_BUCKET")
        out_dir = os.environ.get("LOCAL_OUT")
        if lakehouse or out_dir:
            import pyarrow.parquet as pq

            tail = report.container_metadata.get("tail_number", "unknown")
            prefix = os.environ.get("LAKEHOUSE_PREFIX", "silver")
            from qar_decode.arrow import rate_label

            for rate_hz, table in tables(decoded, fap).items():
                buffer = io.BytesIO()
                pq.write_table(table, buffer, compression="zstd")
                name = f"rate_{rate_label(rate_hz)}hz.parquet"
                if lakehouse:
                    key = f"{prefix}/tail={tail}/run={run_id}/{name}"
                    written.append(_put(lakehouse, key, buffer.getvalue(),
                                        "application/vnd.apache.parquet"))
                if out_dir:
                    path = Path(out_dir) / run_id
                    path.mkdir(parents=True, exist_ok=True)
                    (path / name).write_bytes(buffer.getvalue())
                    written.append(str(path / name))

            buffer = io.BytesIO()
            pq.write_table(report_table(decoded), buffer, compression="zstd")
            if lakehouse:
                written.append(_put(
                    lakehouse, f"{prefix}/_reports/run={run_id}/report.parquet",
                    buffer.getvalue(), "application/vnd.apache.parquet",
                ))

        # 5. Succeed, stamped with the versions that produced it.
        _registry_write("succeeded", {
            **base,
            "decoder_version": DECODER_VERSION,
            "fap": report.fap,
            "container": report.container,
            "tail_number": report.container_metadata.get("tail_number"),
            "flight_number": report.container_metadata.get("flight_number"),
            "origin": report.container_metadata.get("origin"),
            "destination": report.container_metadata.get("destination"),
            "frames": report.n_frames,
            "duration_s": report.duration_s,
            "frame_integrity": report.frame_integrity,
            "sync_confidence": report.sync_confidence,
            "parameters_decoded": len(report.decoded),
            "parameters_failed": len(report.failed),
            "out_of_range": len(report.out_of_range),
            "suspect": [f.mnemonic for f in report.suspect],
            "segments": [asdict(s) for s in segments],
            "segmentation_signal": signal,
            "outputs": written,
            "elapsed_s": round(time.time() - started, 3),
        })
        return EXIT_OK

    except KeyError as exc:
        _registry_write("failed", {**base, "reason": f"missing configuration: {exc}"})
        traceback.print_exc()
        return EXIT_MISCONFIGURED
    except Exception as exc:                              # noqa: BLE001
        _registry_write("failed", {
            **base,
            "reason": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.time() - started, 3),
        })
        traceback.print_exc()
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
