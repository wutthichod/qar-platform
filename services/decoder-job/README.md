# decoder-job

The decode container. I/O only: read bytes, call `qar_decode`, write Parquet
and a registry row. No decoding logic lives here.

## Build

The build context is the **repository root**, because the image installs
`packages/qar-decode` rather than vendoring a copy of it:

```sh
docker build -f services/decoder-job/Dockerfile -t qar-decoder:dev .
```

## Run it locally, with no AWS

`LOCAL_SOURCE`, `LOCAL_FAP` and `LOCAL_OUT` replace the three S3 paths, so
the image runs the same code path against a file on disk:

```sh
docker run --rm \
  -v "$PWD/samples:/data:ro" -v "$PWD/out:/out" \
  -e LOCAL_SOURCE=/data/raw.dat \
  -e LOCAL_FAP=/data/CS77724 \
  -e LOCAL_OUT=/out \
  qar-decoder:dev
```

`tools/local/decode_sample.sh` does this against the POC samples.

## Environment

| Variable | Required | Meaning |
|----------|----------|---------|
| `SOURCE_BUCKET` / `SOURCE_KEY` | yes* | the raw file |
| `FAP_URI` | yes* | `s3://bucket/prefix`, or a `.zip`/`.tar.gz` of the FAP |
| `RAW_ARCHIVE_BUCKET` | no | immutable copy of the raw bytes |
| `LAKEHOUSE_BUCKET` | no | where Silver Parquet lands |
| `LAKEHOUSE_PREFIX` | no | key prefix, default `silver` |
| `REGISTRY_BUCKET` | no | registry rows, defaults to `LAKEHOUSE_BUCKET` |
| `PARAMETERS` | no | comma-separated mnemonics; default is the whole FAP |
| `RUN_ID` | no | defaults to a UTC timestamp |
| `LOCAL_SOURCE` / `LOCAL_FAP` / `LOCAL_OUT` | no | local equivalents of the above |

\* unless the `LOCAL_*` equivalent is set.

## Exit codes

| Code | Meaning | Retry? |
|------|---------|--------|
| 0 | decoded | — |
| 1 | decode failed | yes; transient S3 and truncated files both land here |
| 2 | misconfigured | no; a retry cannot fix a missing variable |

## Output

One Parquet object per **native sample rate**, not one wide table:

```
silver/tail=HS-TTA/run=<RUN_ID>/rate_0p25hz.parquet
silver/tail=HS-TTA/run=<RUN_ID>/rate_1hz.parquet
...
silver/_reports/run=<RUN_ID>/report.parquet
```

Upsampling a 1 Hz parameter to sit beside an 8 Hz one multiplies its storage
eightfold and invents samples that were never recorded. Rates are in hertz;
0.25 Hz is real and common — one sample per four-second frame. Native names, units, bit
widths, word positions, FAP name and decoder version travel as Arrow column
metadata, so any value can be traced back to the bits it came from.

## Fargate sizing

Measured, not estimated: peak resident set and wall clock for a **full-FAP**
decode — every parameter in the document — writing Parquet for each rate.

| Sample | On disk | Unwrapped | Parameters | Peak RSS | Wall | Task |
|--------|---------|-----------|-----------|----------|------|------|
| B777 `raw.dat`, 12h16m | 45 MB | 45 MB | 1,243 | 1.02 GB | 1.1 s | 1 vCPU / 2 GB |
| A350 `.pmf`, 10h18m | 16 MB | 45 MB | 2,650 | 1.78 GB | 1.8 s | 1 vCPU / 4 GB |

Memory is dominated by holding every decoded series as float64 at its native
rate, so it scales with `parameters x rate x duration` and not much with
file size. **Setting `PARAMETERS` to the mnemonics actually wanted is the
lever**: the same A350 file restricted to sixteen mnemonics peaks at 444 MB
in 0.26 s, a quarter of the memory, and fits 0.5 vCPU / 1 GB.

The A350 container also inflates about 5x during gunzip, and both the
compressed and decompressed payloads are briefly resident.

Decode itself is seconds. The wall clock of a real task is dominated by
pulling the image and the FAP, so keep the FAP as a **single archive object**
in S3 — a FAP directory is 2,700 files, and fetching them one request each
costs far more than the decode does.

## Ephemeral storage

The FAP is unpacked under `/tmp`. Fargate gives every task 20 GB by default,
which is ample; set `readonlyRootFilesystem` in the task definition with a
writable mount at `/tmp`.

## Health check

`HEALTHCHECK` synthesises a recording and puts it through the real pipeline
— container, sync, framing, parameters — asserting the values come back. It
touches no network and needs no credentials, so it is also the right first
thing for CI to run against a freshly built image:

```sh
docker run --rm --entrypoint python qar-decoder:dev -m qar_decode.selftest
```
