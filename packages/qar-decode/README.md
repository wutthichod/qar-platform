# qar-decode

Raw recorder bytes in, engineering values out. No cloud dependencies, no
scheduler, no object store — `tests/test_import_boundary.py` keeps it that
way, which is what lets the same code run on a laptop, in CI and in Fargate.

```python
from qar_decode.decode import decode_file
from qar_decode.fap import load_fap
from qar_decode.segment import segment_decoded

fap = load_fap("Fap/CS77724", only={"aALTSTD1", "aCAS1", "aRALTC", "VRTG"})
decoded = decode_file("raw.dat", fap)

print(decoded.report.summary())
print(decoded.by_rate())                 # {4: [...], 20: ['VRTG']}
print(segment_decoded(decoded))          # takeoff and touchdown frames
```

Or from a shell:

```sh
qar-decode raw.dat --fap Fap/CS77724 \
  -p aALTSTD1 -p aCAS1 -p aRALTC --out ./silver --report report.json
```

To read decoded output back — which flight, which FAP, which parameters,
and whether the arithmetic still holds — with no raw file and no FAP:

```sh
qar-decode --inspect ./silver
```

It exits non-zero if anything fails its checks, so it works as a CI gate on
a decode. See `verify.py`.

## Stages

```
container/   strip the recorder's wrapper       -> word stream
arinc717     locate subframes in that stream    -> size and offset
frames       reshape into addressable time      -> frame grid
fap/         read the Safran AGS FAP            -> parameters
parameters   apply a FAP to a frame grid        -> engineering values
segment      find flights inside a recording    -> segments
decode       all of the above, with a report
arrow        decoded values as Arrow tables     (extra: pyarrow)
verify       read a decoded file back           (extra: pyarrow)
selftest     offline end-to-end proof
```

The boundaries are the point. Sync failing to lock is a different problem
from a FAP being wrong, and keeping them separate is what makes a bad decode
diagnosable instead of mysterious. `DecodeReport` names the stage.

## Supported

| Aircraft | Container | Words | Status |
|----------|-----------|-------|--------|
| A350 | ACMS `.pmf` (tar→tar→gzip) | 12-bit, **big**-endian, 1024/subframe | full decode |
| B777 | Teledyne `.wgl/raw.dat` | 12-bit, **little**-endian, 512/subframe | full decode |
| B787 | Boeing EDS crate | CPL record log | container only — see below |

The B787 crate is unwrapped, signature-verified, demuxed into its 1/5/10 Hz
record streams and identified, but its payloads are not ARINC 717 and
parameter extraction needs Boeing's CPL map. `unwrap` raises
`UnsupportedContainer` rather than inventing a word stream.

`../../docs/formats.md` has the full derivation of all three formats and the
FAP schema.

## Output shape

One table per **native sample rate**, not one wide table at the maximum. A
1 Hz parameter upsampled to sit beside an 8 Hz one costs 8x the storage and
invents samples that were never recorded. Native names, units, bit widths,
word positions, FAP name and decoder version travel as Arrow column
metadata, so any value can be traced back to the bits it came from.

## Timing

A **subframe is one second and a frame is four**, per ARINC 717. The
constants live in `frames.py` and everything scales off them. This is worth
knowing before reading a rate: AGS `PRM_RATE` counts samples *per frame*, so
`PRM_RATE=32` on VRTG is 8 Hz, not 32. `Series` exposes both
`samples_per_frame` and `rate_hz`, and segments are indexed in seconds with
`*_frame` properties for comparing against a vendor tool.

## Two things the decoder decides for itself

**Signedness.** `PRM_PARAMETER_SIGNED` is set on 5 of the A350's 2,693
parameters, and pitch, heading and lateral acceleration are not among them
though all three are two's complement. Where `PRM_MIN_OP_RANGE` is negative,
both readings are tried and the one that puts more samples inside the
declared range wins.

**Fitness.** Two checks run on every decode and appear in the report:
values outside the FAP's declared operating range, and continuous
parameters whose consecutive samples jump by more than a quarter of that
range too often. The second is what catches a FAP revision that has drifted
from the recording — a mis-mapped parameter usually stays inside its
declared range, so only the jump test finds it.

## Tests

```sh
conda env create -f ../../environment.yml     # once
conda activate qar-decode
pytest tests
```

Or without conda: `pip install -e '.[dev]'`.

Everything is synthesised except `tests/test_samples.py`, which regresses
against the real POC files and skips when they are absent. Real flight data
is operationally sensitive and is not in this repository; point
`QAR_SAMPLES` at a `qar-data` checkout to run those.
