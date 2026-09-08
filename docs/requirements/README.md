# Requirements

FR-1 to FR-8 come from QAR_Data_Platform_Proposal.docx. This directory
tracks what satisfies each and what is still open.

| FR | Subject | Satisfied by | Status |
|----|---------|--------------|--------|
| FR-1 | Ingestion | S3 landing, event notification, SQS | Infra built |
| FR-2 | Raw retention | Versioned bucket, Object Lock | Infra built |
| FR-3 | Decode | packages/qar-decode | **Blocked** |
| FR-4 | Time-series storage | Iceberg Silver tables | **Blocked on FR-5** |
| FR-5 | Query layer | Athena or Trino | **Open** |
| FR-6 | Events and phases | Gold layer | Not started |
| FR-7 | Fleet analysis | Gold aggregates | Not started |
| FR-8 | Field-level access | Lake Formation column grants | Infra built, unverified |

## Blockers

FR-3 needs two artefacts from the FDM vendor:
- a raw QAR binary file
- FAP document CS78761-1-0 v1.00

Without the first, the container format and packing are unknown. Without
the second, word positions and conversions are unknown.

`packages/qar-decode/tests/fixtures/THA935-1_decoded.csv` is the known-good
output to validate against once both arrive.
