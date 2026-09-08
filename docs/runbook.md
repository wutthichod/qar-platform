# Apply and test runbook

Order matters here. Two steps in the middle will fail if done in the wrong
sequence, and one Lake Formation setting will break the first decode if left
on.

## Before you start

- [ ] **Region confirmed** against the FOQA agreement and any data residency
      requirement. Changing it later means moving data, not editing a variable.
- [ ] A VPC with at least two subnets, and their IDs to hand.
- [ ] AWS CLI authenticated as a principal that can create IAM roles.
- [ ] Docker running locally, for building the decode image.
- [ ] A billing alarm set. Athena charges per byte scanned.

## Step 1 — comment out the Lake Formation location registration

In `catalog.tf`, temporarily comment out `aws_lakeformation_resource.lakehouse`.

**Why:** registering an S3 location with Lake Formation switches that path to
Lake Formation credential vending. Until grants are correct, the decode role's
IAM permissions stop being sufficient on their own and the first decode fails
with an opaque access-denied. Get the pipeline green on plain IAM first, then
layer Lake Formation on in step 7. Debugging both at once is miserable.

## Step 2 — apply

```bash
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars          # vpc_id, subnet_ids, region

tofu init
tofu plan -out=tfplan             # read it; roughly 45 resources
tofu apply tfplan
```

Expect two to three minutes. The Batch compute environment is the slow one.

## Step 3 — relax ECR tag immutability for the first push

The job definition references `:latest`, but the repo is created with
`IMMUTABLE` tags, so `:latest` can only ever be pushed once. For testing,
either set `image_tag_mutability = "MUTABLE"` in `compute.tf` and re-apply, or
push a version tag and update the job definition to match.

Production answer is the second one. Testing answer is the first.

## Step 4 — build and push the decode image

```bash
REPO=$(tofu output -raw decoder_repository_url)
REGION=$(grep '^region' terraform.tfvars | cut -d'"' -f2)

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REPO"

docker build -t "$REPO:latest" ./decoder
docker push "$REPO:latest"
```

The stub decoder is not an ARINC 717 decoder. It reads the file, archives it,
writes 600 synthetic rows to an Iceberg table, and records a registry row. That
is enough to prove every hop in the infrastructure.

## Step 5 — run the smoke test

```bash
./scripts/smoke_test.sh
```

It uploads a fake file, waits for Batch, waits for the job, queries the
registry through Athena, and checks the DLQ is empty.

## Step 6 — failures you should expect, in likelihood order

**No job appears within five minutes.**
The Lambda didn't fire or couldn't submit.
```bash
aws logs tail /aws/lambda/qar-submit-job --follow --region "$REGION"
```
Usually the S3 notification or the queue policy. Confirm a message actually
landed: check `ApproximateNumberOfMessages` on the decode queue.

**Job stuck in `RUNNABLE` forever.**
Batch can't place the task. Almost always networking: the subnets have no
route to pull from ECR. Either set `assign_public_ip = true` with public
subnets, or add VPC endpoints for ECR API, ECR DKR, S3 and CloudWatch Logs.
This is the single most common Batch-on-Fargate failure.

**Job `FAILED` with an image pull error.**
Nothing was pushed to ECR, or the tag doesn't match. See step 4.

**Job `FAILED` with access denied on Glue.**
The decode role can create tables in `qar_silver` only. Confirm `GLUE_DATABASE`
in the job definition matches, and that Lake Formation isn't intercepting —
see step 1.

**Athena query fails with "table does not exist".**
The decoder creates tables on first run. If the job failed, no table exists.
Fix the job first.

## Step 7 — layer Lake Formation on

Only once the pipeline is green end to end.

1. Uncomment `aws_lakeformation_resource.lakehouse` and re-apply.
2. Re-run the smoke test. If decode now fails, the decode role needs its
   grants checked — it already has `ALL` on the Silver database, but the
   registered location may need a `DATA_LOCATION_ACCESS` grant too.
3. Set `silver_flights_table = "flight_params"` and re-apply to activate the
   column-level grant.

## Step 8 — prove FR-8 actually holds

This is the test that matters, and the one most likely to be skipped.

```bash
ANALYST=$(tofu output -raw analyst_role_arn)
CREDS=$(aws sts assume-role --role-arn "$ANALYST" \
  --role-session-name fr8-test --query Credentials --output json)

export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | python3 -c 'import json,sys;print(json.load(sys.stdin)["AccessKeyId"])')
export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | python3 -c 'import json,sys;print(json.load(sys.stdin)["SecretAccessKey"])')
export AWS_SESSION_TOKEN=$(echo "$CREDS" | python3 -c 'import json,sys;print(json.load(sys.stdin)["SessionToken"])')
```

Two assertions, both of which must hold:

**A. Direct object access is refused.**
```bash
aws s3 ls "s3://$(tofu output -raw lakehouse_bucket)/"     # must fail
```
If this succeeds, the whole access-control design is decoration.

**B. Restricted columns are absent, not null.**
Run `SELECT * FROM qar_silver.flight_params LIMIT 1` through the analyst
workgroup. The columns in `restricted_columns` should not appear in the result
schema at all.

Unset the three environment variables afterwards.

## Step 9 — tear down

```bash
tofu destroy
```

**It will fail**, and that's by design. The raw archive bucket has Object Lock
under `GOVERNANCE` mode with a ten-year default retention, so its objects can't
be deleted normally. For a test environment, delete the objects with
`s3:BypassGovernanceRetention`, then re-run destroy. In production this
behaviour is the point.

Buckets holding data will also block destroy until emptied — also deliberate.

## What this does not test

Cost at volume, query performance, Spot interruption behaviour, concurrent
write contention on Iceberg, or anything about correctness of decoding. It
proves the plumbing, nothing more.
