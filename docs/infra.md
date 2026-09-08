# QAR Data Platform — infrastructure

OpenTofu configuration for the QAR/FOQA lakehouse. Validated against the
`hashicorp/aws` v6 provider with `tofu validate`, but **never applied against a
real account** — treat it as a reviewed starting point, not finished work.

## What it creates

| File | Contents |
|---|---|
| `storage.tf` | KMS key, landing / raw-archive / lakehouse / extracts / athena-results buckets |
| `queue.tf` | SQS decode queue, dead-letter queue, S3 event notification |
| `compute.tf` | ECR repo, IAM roles, Batch compute environment, two job queues, job definition |
| `submit.tf` | Lambda that turns a queue message into a `SubmitJob` call |
| `catalog.tf` | Glue databases for bronze/silver/gold, Lake Formation settings and grants |
| `query.tf` | Athena workgroup with a scan cap, and the analyst role |

## The design decisions encoded here

**Three data buckets, not one.** Retention, encryption and access policy differ
per layer. Raw archive has Object Lock and versioning because re-decode from
original bytes is a hard requirement; extracts expire after 30 days.

**Two Batch job queues on one compute environment.** `qar-decode` at priority
100 for today's arrivals, `qar-backfill` at priority 1 for sweeps and
re-decodes. A six-month re-decode can never delay a flight that landed an hour
ago.

**`batch_max_vcpus` is your cost ceiling.** It is the thing that stops a
runaway backfill from launching a fleet.

**The analyst role has an explicit `Deny` on lakehouse objects.** Not merely an
absent `Allow` — a `Deny`, so a later policy edit cannot quietly open the hole
that FR-8 depends on staying closed. The query engine is the only credentialed
path to the data.

**The Lambda is deliberately trivial.** It translates a message into a
`SubmitJob` call and nothing else. All decode logic lives in the container, so
the pipeline stays substrate-agnostic and moving off Batch later is a
configuration change rather than a rewrite.

## Deploying

```bash
cp terraform.tfvars.example terraform.tfvars
# fill in vpc_id and subnet_ids, and confirm the region against
# your data residency requirements

tofu init
tofu plan
tofu apply
```

Then push a decode image before submitting any jobs — the job definition
references `:latest` in the ECR repo and will fail without it:

```bash
aws ecr get-login-password --region ap-southeast-1 \
  | docker login --username AWS --password-stdin "$(tofu output -raw decoder_repository_url)"

docker build -t "$(tofu output -raw decoder_repository_url):latest" ./decoder
docker push "$(tofu output -raw decoder_repository_url):latest"
```

Note the ECR repo is set to `IMMUTABLE` tags, so `:latest` can only be pushed
once. Use version tags in practice and parameterise the job definition.

## Applying the column-level grant

`silver_flights_table` is empty by default because Lake Formation cannot grant
on a table that does not exist. Once the decoder has created the Silver table,
set the variable and re-apply:

```hcl
silver_flights_table = "flight_params"
restricted_columns   = ["crew_id", "captain_name", "first_officer_name"]
```

Then verify enforcement by assuming the analyst role and running a `SELECT *`.
The restricted columns should be absent, not merely null.

## What is deliberately not here

- **VPC.** You supply `vpc_id` and `subnet_ids`. Creating a VPC with a NAT
  gateway adds meaningful monthly cost and most organisations already have one.
- **Dagster.** Runs outside this module. Its IAM role needs `batch:SubmitJob`
  on the backfill queue and read access to the Glue catalog.
- **Iceberg tables.** Created by the decoder at runtime, not by OpenTofu.
  Table schemas belong in application code, not infrastructure.
- **Remote state backend.** Add an S3 backend with DynamoDB locking before
  more than one person runs this.
- **Environment separation.** Deploy twice with different `project` values, or
  wrap in a module and call it per environment.

## Cost shape when idle

Close to zero. Batch scales to no capacity when the queue is empty, Athena
charges per query, and Lambda charges per invocation. The standing costs are
KMS (about $1/month per key), S3 storage, and CloudWatch log retention.

The variable costs to watch are Athena scans — hence the workgroup cap — and
NAT gateway data processing if you move tasks into private subnets.

## Before production

- [ ] Confirm region against data residency and the FOQA agreement
- [ ] Move Batch tasks to private subnets, set `assign_public_ip = false`
- [ ] Add VPC endpoints for S3, ECR and CloudWatch Logs
- [ ] Switch `raw_archive` Object Lock from `GOVERNANCE` to `COMPLIANCE` if
      retention must be genuinely immutable
- [ ] Add a remote state backend with locking
- [ ] Add CloudWatch alarms on DLQ depth and Batch job failure count
- [ ] Review whether `batch:SubmitJob` on `Resource = "*"` can be narrowed
- [ ] Decide Athena vs Trino — if Trino, `query.tf` is replaced entirely
