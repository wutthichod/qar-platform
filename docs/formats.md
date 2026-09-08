# Recorder and FAP formats

What the POC sample set turned out to contain, and how each piece was
established. Written down because none of it is documented publicly and all
of it was determined from the bytes.

Everything here is verified against the files in `decoder-poc/`. The
regression tests in `packages/qar-decode/tests/test_samples.py` pin the
findings; they skip when the samples are absent.

---

## 1. The FAP: Safran AGS

A FAP is a directory, not a file.

```
CS350THA22/
  A350THA22.lframe                  frame geometry
  CS350THA22.lfapproj               MSBuild project listing every document
  CS350THA22.solution               Visual Studio solution wrapper
  AcquiredParameters/*.lacquired    one XML per parameter  (2,693 for the A350)
  DerivedParameters/*.lderived      computed parameters    (not yet used)
  Events/, KeyValues/               FDM event definitions  (not yet used)
```

**Encoding trap.** Every document declares `encoding="utf-16"` in its XML
prolog and is in fact UTF-8 with a BOM. The declaration is wrong. Decode as
`utf-8-sig`; trusting the prolog produces mojibake rather than an exception,
so the failure surfaces much later as unexplained parameter names.

### `.lframe` — frame geometry

| Attribute | Meaning |
|-----------|---------|
| `FRA_SUBFRAME_WORD_NB` | words per subframe (256 / 512 / 1024 / 4160 here) |
| `FRA_NB_FRAMES_PER_SUPERFRAME` | 16 throughout |
| `FRA_SFC_LOC_F` / `_LOC_W` / `_BIT_SOURCE_LSB` / `_BIT_LENGTH` | superframe counter location |
| `FRA_DFC_*` | frame counter location |

Observed across the eight FAPs in the sample set:

| FAP | Frame | Subframe words | Aircraft |
|-----|-------|----------------|----------|
| `CS320THA21`, `A320THA21` | A320THA21 | 256 | A320 |
| `CS330THA21` | A330THA | 512 | A330 |
| `CS350THA22` | A350THA22 | 1024 | A350 |
| `CS350THA24` | A350THA24 | 1024 | A350 |
| `CS73726` | B734A | 256 | B737-400 |
| `CS77724` | THA773Q-a | 512 | B777 |
| `CS77726` | QARGEF04-d | 512 | B777 |
| `CS78732` | BCG4BRR26 | 4160 | B787 |

### `.lacquired` — one parameter

Three element types under the `<PRA>` root:

- **`<LCP>`** — a *location part*: `LCP_WORD_NB` (1-based within the
  subframe), `LCP_SUBFRAME` (`ALL`, a number, or a list like `1,3`),
  `LCP_BIT_SOURCE_LSB`, `LCP_BIT_LENGTH`, `LCP_BIT_TARGET_LSB`,
  `LCP_PART_NUMBER`, `LCP_SAMPLE_NUMBER`, `LCP_MODULO`.
- **`<EUC>`** — conversion. `value = Σ EUC_COEFF_n × 10^EUC_COEFF_EXP_n × raw^n`.
  Usually linear. A parameter may carry several, each valid over its own
  `EUC_MIN`/`EUC_MAX` raw range.
- **`<TXD>`** — a discrete's value-to-label mapping.

Four things about `<LCP>` are easy to get wrong, and each produces
plausible-looking numbers rather than an error:

1. **`LCP_BIT_TARGET_LSB = 0` does not mean bit 0.** It means "continue
   immediately above the previous part". Reading it literally ORs every part
   on top of bit 0.

2. **Parts of one sample can live in different subframes.** A350 `GW` keeps
   twelve bits in subframe 3 and the top three in subframe 4, under one
   `LCP_SAMPLE_NUMBER`. Grouping by subframe splits one 15-bit parameter
   into a 12-bit and a 3-bit one, both of which decode to something.

3. **The rate is `len(subframes) × len(samples)`**, not a number to trust.
   `PRM_RATE` agrees on every non-superframe parameter checked, and the
   derived value is what the decoder uses.

4. **`PRM_PARAMETER_SIGNED` is almost never set** — 5 parameters out of
   2,693 on the A350 — yet pitch, heading and lateral acceleration are all
   two's complement. The decoder decides from `PRM_MIN_OP_RANGE`: when it is
   negative, both interpretations are tried and the one that puts more
   samples inside the declared range wins. Read unsigned, a nose-down pitch
   becomes about +180°.

### Superframe parameters

`PRA_SUPERFRAME="1"` with `LCP_MODULO="n"`: the parameter occupies its word
only on the frame whose superframe counter reads `n`, so one word carries
sixteen different parameters across the cycle.

**`LCP_MODULO` maps onto the superframe counter directly, with no offset.**
Confirmed unambiguously on the A350: subframe 1, word 586 read at modulos
0–14 spells

```
sfc  0  1  2  3  4  5  6  7   8  9 10 11 12 13 14
      V  T  B  S  O  P  K  C   .  H  S  -  T  H  J
      └── VTBS ──┘└── OPKC ┘      └── HS-THJ ──┘
```

— the departure and destination airports and the tail number, matching the
container manifest, which was parsed by a completely separate code path.

---

## 2. A350 — Airbus ACMS `.pmf`

Three wrappers, each a standard format under an unusual extension:

```
.pmf  ->  tar   {manifest.xml, inner.tar}
inner ->  tar   {RECORDING.gz}
member->  gzip  ->  ACMS payload
```

The manifest uses single-letter tags: `<n>` the ACMS node (`ACMS.HS-THJ`),
`<t>` flight number, `<f>` recording id, `<s>` offload time.

The payload opens with a variable-length self-describing header (channel
names, an embedded parameter dictionary, route and tail in ASCII) and then
runs ARINC 717 to the end of the file.

- **Words are 12-bit in big-endian 16-bit slots.** The B777 recorder in the
  same fleet is little-endian; a decoder that hardcodes one silently
  produces garbage for the other.
- The header is not a fixed size. Sync search finds the first frame, which
  is needed anyway. In the sample it lands at word 2046.
- `HS-THJ` sample: 1024-word subframes, 22,019 subframes, **sync confidence
  1.000000, zero bad subframes.**

**Recorded UTC beats the container timestamp.** The manifest says
2022-03-27 (offload); the recording's own `YEAR`/`MONTH`/`DAY`/`UTC_*`
parameters say 2022-03-22 11:13:10, which matches the timestamp embedded in
the filename (`QAR000120002203221113 11`) to the second.

---

## 3. B777 — Teledyne `.wgl/raw.dat`

The plainest: 12-bit words in **little-endian** 16-bit slots from byte zero,
512-word subframes, matching `CS77724`/`CS77726`.

**The pad nibble is not a gap.** Each word sits in a 16-bit slot with four
bits spare, and the recorder does not write them consistently — most of the
file leaves them clear, a few multi-kiloword runs have them all set. It is
tempting to read those runs as unwritten flash. They are not: their contents
vary and every one begins on a subframe boundary with a valid sync word.
Mask the pad bits and change nothing else.

`HS-TKN` opens with 5,632 words that are not frames, so its first subframe
boundary is at word 6144. A sync search bounded by one subframe never
reaches it and reports the file unreadable.

Both samples: **sync confidence 1.000000, frame integrity 100%**, 1,243
parameters decoded, rates 1/2/4/8/16/20/40 Hz.

---

## 4. B787 — Boeing EDS crate (**not decodable yet**)

Outer layer is a zip holding `crate.xml` — an XML-DSig manifest naming the
payload with its SHA-256 — beside the payload. The digest verifies on both
samples, and the decoder refuses a payload the manifest did not sign.

The payload is a **CPL** (Continuous Parameter Logging) stream:

```
EB 90 | length u16 | sequence u32 | type u16 | payload
```

`length` covers the whole record. The type census over a full flight is the
interesting part:

| Type | Records | Payload | Meaning |
|------|---------|---------|---------|
| 1 | 1 | 130 B | header: CPL map id, tail |
| 3 | *n* | 4201 B | **1 Hz** parameter block |
| 4 | 5*n* | 237 B | **5 Hz** block |
| 5 | 10*n* | 11 B | **10 Hz** block |
| 7 | ~*n*/4 | 29 B | events |
| 8 | few | 57 B | flight identity: tail, route, flight number |

The 1 : 5 : 10 ratio is exact on both samples. So the mixed-rate structure
that Silver has to represent is visible in the container before a single
parameter is extracted.

**Why it stops there.** CPL payloads are not ARINC 717. There is no subframe
sync anywhere in them at any packing — big- and little-endian 16-bit and
both 12-bit packings were checked across 95 MB of type-3 payload, at every
candidate subframe size. Extracting parameters needs Boeing's CPL map, named
in the type 1 record as `647A-00 CPL_MAP_RR01AA-906`, which is not in the
FAP set. `CS78732` (4160-word frames) describes a different 787 recorder
source, not this CPL crate.

Until that map is available, `container.crate` unwraps, verifies, demuxes
and reports — and `unwrap` raises `UnsupportedContainer` rather than
inventing a word stream that would decode to convincing nonsense.

---

## 5. What is known to be imperfect

The A350 decode is validated against physics on the parameters that matter
(vertical acceleration averaging 1.000 g, N1 topping at 102.4%, altitude
96 → 43,036 ft, heading ±180°). Two known problems, both consistent with the
FAP revision having drifted from the recording:

- **`IAS` and `CAS_CAP` are mis-mapped** under both `CS350THA22` and
  `CS350THA24`: they alternate between 0 and 384 kt at 4 Hz. The decoder's
  discontinuity check flags exactly these two and leaves the good
  parameters alone. Use `GS` for ground speed until the matching FAP
  revision is obtained.
- **`GW`** decodes to a smoothly varying but implausible magnitude, and
  `AC_GROUND` / `ANY_GEAR_GND` read a constant 0 with ~25% no-computed-data,
  contradicting an altitude trace that starts and ends on the ground.

Both are worth raising with Safran alongside the question of which FAP
revision matches a 2022 A350 recording. Nothing about them affects the
container, sync, framing or bit-extraction layers, all of which are exact.
