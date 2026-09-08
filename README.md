# QAR Data Platform

Ingests raw QAR binary files (ARINC 717), decodes them using aircraft-specific
frame layout documents, and stores decoded flight time-series in a governed
lakehouse for FOQA/FDM analysis.

## Layout

```
packages/qar-decode      the product. bytes in, parameters out. no AWS.
packages/qar-schema      canonical dictionary, table definitions
packages/qar-client      analyst library. no raw SQL accepted.
services/decoder-job     the container. I/O only.
orchestration/           Dagster. submits jobs, records outcomes.
infra/modules/           OpenTofu module
infra/envs/{dev,prod}    per-environment stacks, separate state
tools/                   local stack, smoke test
docs/formats.md          recorder and FAP formats, as reverse-engineered
docs/decisions/          why things are the way they are
```

## Data layers

| Layer | Contents | Trigger |
|-------|----------|---------|
| Bronze | Raw bytes + file registry | S3 event |
| Bronze+ | Per-flight segments, versioned | S3 event |
| Silver | Decoded parameters, native rates, canonical names | Event **and** sweep |
| Gold | Phases, events, derived, fleet aggregates | Watermark micro-batch |

## Principles

**Registry before work.** The registry row is written before decoding starts,
so a crashed job leaves a visible record rather than silence.

**Version-stamp every row.** Decoder version and FAP version on every Silver
row, so a FAP correction is a re-decode of a known subset rather than an
archaeology project.

**Never destroy native names.** Canonical names become column names; native
names, units, word positions and versions survive as column metadata.

**Explicit Deny, not absent Allow.** See docs/decisions/0004.

**Sweep alongside events.** S3 events cannot catch FAP corrections, decoder
upgrades, or files whose event was lost. The registry sweep can.

**Substrate-agnostic containers, thin orchestrator.** No scheduler SDK in the
decoder, no logic in Dagster. Enforced by
`packages/qar-decode/tests/test_import_boundary.py`.

## Status

Infrastructure is built and validated. The decoder now decodes.

The `qar-data/` sample set supplied the raw files and the Safran AGS FAPs
that were previously missing. Against them:

| Aircraft | Container | Result |
|----------|-----------|--------|
| A350 | ACMS `.pmf` | 1024-word subframes, sync confidence 1.000000, 100% frame integrity, 6h07m |
| B777 | Teledyne `.wgl` | 512-word subframes, sync confidence 1.000000, 100% frame integrity, 1,243 parameters, 12h16m |
| B787 | Boeing EDS crate | unwrapped, signature-verified and demuxed; **parameters blocked** |

Values are validated against physics rather than against a claim: vertical
acceleration averages 1.000 g over a flight, N1 tops out at 102.4%, altitude
runs 96 → 43,036 ft, heading stays within ±180°. The strongest single check
is the A350 superframe, where word 586 read at the modulos the FAP gives
spells the tail number and both airport codes in ASCII — independently
matching the container manifest.

Timing is anchored to the recording's own clock rather than assumed: a
subframe is one second and a frame is four, confirmed by UTC advancing
4.000 s across all 5,504 frame transitions of the A350 sample, with zero
drift between the decoded time axis and that clock over 22,020 rows.

`docs/formats.md` derives all three container formats and the FAP schema.

**Still blocked:**
- **B787 parameters.** CPL payloads are not ARINC 717. Extraction needs
  Boeing's CPL map `647A-00 CPL_MAP_RR01AA-906`, named in the file and not
  in the FAP set.
- **A350 FAP revision.** `IAS`, `CAS_CAP` and `GW` decode implausibly under
  both `CS350THA22` and `CS350THA24` while everything around them is exact —
  the signature of a FAP revision that has drifted from the recording. The
  decoder flags them automatically; the fix is the matching revision from
  Safran.
- FAP `CS78761-1-0 v1.00` and the raw file matching
  `packages/qar-decode/tests/fixtures/THA935-1_decoded.csv`, which remains
  the column-for-column regression fixture for the vendor's own output.

The open decision blocking FR-4 is Athena vs Trino. See docs/decisions/0002.
The sample set answers the schema half of it: rates are genuinely mixed —
0.25/1/4/8 Hz on the A350, 0.25/0.5/1/2/4/5/10 Hz on the B777 — so Silver
stores one table per native rate rather than one wide table at the maximum.

## Getting started

```bash
# one-time: create the environment (python 3.12, matching the container)
conda env create -f environment.yml
conda activate qar-decode

# decoder tests, no AWS account needed
cd packages/qar-decode && python -m pytest tests -q

# decode any file directly
qar-decode <raw file> --fap <FAP dir> -p ALT_STD -p VRTG --out ./silver

# read decoded output back: what flight, what parameters, does it check out
qar-decode --inspect ./silver

# decode a POC sample end to end, no AWS account needed
tools/local/decode_sample.sh b777
tools/local/decode_sample.sh a350
tools/local/decode_sample.sh b787      # container inspection only

# build the Fargate image (context is the repo root)
docker build -f services/decoder-job/Dockerfile -t qar-decoder:dev .

# infrastructure
cd infra/envs/dev && tofu init && tofu plan
```

See docs/runbook.md before applying anything.
