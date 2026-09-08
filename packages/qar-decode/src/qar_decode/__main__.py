"""``python -m qar_decode`` -- decode a file on a laptop.

The same code path the container runs, minus the object store. If a file
decodes here it will decode in Fargate; if it does not, the report says
which stage gave up.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="qar_decode", description="Decode a raw QAR file against a FAP."
    )
    ap.add_argument(
        "source", type=Path,
        help="raw QAR file (.pmf, raw.dat, crate zip), or with --inspect a "
             "decoded .parquet file or a directory of them",
    )
    ap.add_argument("--fap", type=Path, help="FAP directory (required to decode)")
    ap.add_argument(
        "--inspect", action="store_true",
        help="read a decoded file back and report what it is, instead of "
             "decoding. Needs no FAP and no raw file.",
    )
    ap.add_argument(
        "-p", "--parameter", action="append", default=[],
        help="mnemonic to decode; repeatable. Default: every parameter in the FAP.",
    )
    ap.add_argument("--out", type=Path, help="directory for one Parquet file per rate")
    ap.add_argument("--report", type=Path, help="write the decode report as JSON")
    ap.add_argument("--limit", type=int, default=0, help="stop after N parameters")
    args = ap.parse_args(argv)

    if args.inspect:
        return _inspect(args.source)

    if args.fap is None:
        ap.error("--fap is required to decode (or pass --inspect)")

    from qar_decode.decode import decode_file
    from qar_decode.fap import load_fap

    only = set(args.parameter) or None
    fap = load_fap(args.fap, only=only)
    if args.limit and only is None:
        keep = sorted(fap.parameters)[: args.limit]
        only = set(keep)

    try:
        decoded = decode_file(args.source, fap, only=only)
    except Exception as exc:                          # noqa: BLE001
        print(f"decode failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(decoded.report.summary())
    print()
    for rate_hz, names in decoded.by_rate().items():
        rows = int(round(rate_hz * decoded.report.duration_s))
        print(f"  {rate_hz:>6g} Hz  {len(names):>4} parameter(s)  {rows:>9} rows")

    if args.out:
        import pyarrow.parquet as pq

        from qar_decode.arrow import tables

        args.out.mkdir(parents=True, exist_ok=True)
        from qar_decode.arrow import rate_label

        for rate_hz, table in tables(decoded, fap).items():
            path = args.out / f"rate_{rate_label(rate_hz)}hz.parquet"
            pq.write_table(table, path, compression="zstd")
            print(f"  wrote {path}  ({table.num_rows} x {table.num_columns})")

    if args.report:
        args.report.write_text(_report_json(decoded))

    return 0


def _inspect(path: Path) -> int:
    """Report on already-decoded output."""
    from qar_decode.verify import inspect_dir

    try:
        infos = inspect_dir(path)
    except Exception as exc:                          # noqa: BLE001
        print(f"cannot read {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not infos:
        print(f"no .parquet files under {path}", file=sys.stderr)
        return 1

    for i, info in enumerate(infos):
        if i:
            print("\n" + "-" * 72 + "\n")
        print(info.summary())

    problems = sum(len(i.problems) for i in infos)
    if len(infos) > 1:
        rows = sum(i.n_rows for i in infos)
        params = sum(
            len([c for c in i.columns if c.name not in ("t_offset_s", "timestamp", "frame")])
            for i in infos
        )
        print(f"\n{'=' * 72}")
        print(f"{len(infos)} file(s), {rows:,} rows, {params} parameter columns, "
              f"{problems} problem(s)")
    return 1 if problems else 0


def _report_json(decoded) -> str:
    r = decoded.report
    return json.dumps(
        {
            "source": r.source,
            "container": r.container,
            "container_metadata": r.container_metadata,
            "fap": r.fap,
            "decoder_version": r.decoder_version,
            "subframe_words": r.subframe_words,
            "sync_offset": r.sync_offset,
            "sync_confidence": r.sync_confidence,
            "frames": r.n_frames,
            "duration_s": r.duration_s,
            "frame_integrity": r.frame_integrity,
            "rates_hz": {f"{k:g}": v for k, v in decoded.by_rate().items()},
            "decoded": r.decoded,
            "failed": r.failed,
            "out_of_range": [
                {
                    "mnemonic": f.mnemonic,
                    "unit": f.unit,
                    "declared": list(f.declared),
                    "observed": list(f.observed),
                    "fraction_outside": f.fraction_outside,
                }
                for f in r.out_of_range
            ],
            "suspect": [
                {
                    "mnemonic": f.mnemonic,
                    "unit": f.unit,
                    "rate_hz": f.rate_hz,
                    "jump_fraction": f.jump_fraction,
                    "declared_span": f.declared_span,
                }
                for f in r.suspect
            ],
            "notes": r.notes,
            "decoded_at": r.started_at,
            "elapsed_s": r.elapsed_s,
        },
        indent=2,
        default=str,
    )


if __name__ == "__main__":
    raise SystemExit(main())
