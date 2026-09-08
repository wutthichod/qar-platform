# Fixtures

## THA935-1_decoded.csv

Real decoded output. THA935-1, HS-TQC, Brussels to Bangkok, 2025-12-01.
38,592 rows, one per second, 10.7 hours including taxi at both ends.
Parameter map CS78761-1-0 v1.00, format "test cu" v1.00, 16 parameters.

The header carries takeoff at frame 126 and touchdown at frame 9588 --
the flight boundaries that segmentation must reproduce.

This is the regression fixture for the vendor's own output. The matching
raw file is still not available; `tests/test_samples.py` regresses against
the `decoder-poc` sample set instead, which is a different set of flights.
Once the matching raw file arrives, decoding it must reproduce this CSV
column for column. The strongest check
is `_VRTG` at 22:49:55 -- ten samples spanning 0.79 to 1.25 G, the landing
impact.

### What it establishes

Sample rates are genuinely mixed, and one is an illusion:

| Parameter | Word slots | Distinct values | True rate |
|-----------|-----------|-----------------|-----------|
| `_VRTG` | 10 | varies in 54% of rows | 10 Hz |
| `_PITCH` | 5 | varies in 13% of rows | 5 Hz |
| `_IVV` | 5 | identical in 100% of rows | **1 Hz** |

Storing `_IVV` as 5 Hz would inflate Silver fivefold for no information.
This is the concrete case behind the FR-4 schema shape question.

`Status` is present in the format and empty throughout, so quality flags
must be derived rather than read.

`FLIGHT_PHASE` is already computed by the vendor tool -- useful as ground
truth to validate the Gold layer against.

### Handling

Real flight data is operationally sensitive. Before this repository is
shared, either move fixtures to a private location or anonymise the tail
number and route.
