#!/usr/bin/env bash
# End-to-end pipeline smoke test.
#
# Uploads a fake QAR file, then follows it through the queue, Batch, Iceberg
# and Athena. Run from the module root after `tofu apply`.

set -euo pipefail

REGION="$(tofu output -raw -state=terraform.tfstate 2>/dev/null || true)"
REGION="${AWS_REGION:-ap-southeast-1}"

LANDING="$(tofu output -raw landing_bucket)"
DLQ_URL="$(tofu output -raw decode_dlq_url)"
JOB_QUEUE="$(tofu output -raw batch_job_queue)"
WORKGROUP="$(tofu output -raw athena_workgroup)"
SILVER="$(tofu output -json glue_databases | python3 -c 'import json,sys; print(json.load(sys.stdin)["silver"])')"

KEY="smoke/$(date +%Y%m%d-%H%M%S).qar"

echo "==> 1. uploading a fake QAR file to s3://${LANDING}/${KEY}"
head -c 65536 /dev/urandom > /tmp/fake.qar
aws s3 cp /tmp/fake.qar "s3://${LANDING}/${KEY}" --region "$REGION"

echo "==> 2. waiting for Batch to pick it up (up to 5 minutes)"
JOB_ID=""
for _ in $(seq 1 30); do
  JOB_ID="$(aws batch list-jobs \
    --job-queue "$JOB_QUEUE" \
    --region "$REGION" \
    --query 'jobSummaryList[0].jobId' \
    --output text 2>/dev/null || true)"
  [ -n "$JOB_ID" ] && [ "$JOB_ID" != "None" ] && break
  sleep 10
done

if [ -z "$JOB_ID" ] || [ "$JOB_ID" = "None" ]; then
  echo "!! no job appeared. Check the Lambda logs:"
  echo "   aws logs tail /aws/lambda/qar-submit-job --follow --region $REGION"
  exit 1
fi
echo "    job: $JOB_ID"

echo "==> 3. waiting for the job to finish"
for _ in $(seq 1 60); do
  STATUS="$(aws batch describe-jobs --jobs "$JOB_ID" --region "$REGION" \
    --query 'jobs[0].status' --output text)"
  echo "    status: $STATUS"
  case "$STATUS" in
    SUCCEEDED) break ;;
    FAILED)
      aws batch describe-jobs --jobs "$JOB_ID" --region "$REGION" \
        --query 'jobs[0].{reason:statusReason,container:container.reason}'
      echo "!! decode failed. Container logs:"
      echo "   aws logs tail /aws/batch/qar-decode --follow --region $REGION"
      exit 1
      ;;
  esac
  sleep 10
done

echo "==> 4. querying the registry through Athena"
QID="$(aws athena start-query-execution \
  --region "$REGION" \
  --work-group "$WORKGROUP" \
  --query-string "SELECT source_key, status, decoder_version, fap_version FROM ${SILVER}.file_registry ORDER BY processed_at DESC LIMIT 5" \
  --query 'QueryExecutionId' --output text)"

for _ in $(seq 1 30); do
  STATE="$(aws athena get-query-execution --query-execution-id "$QID" \
    --region "$REGION" --query 'QueryExecution.Status.State' --output text)"
  [ "$STATE" = "SUCCEEDED" ] && break
  [ "$STATE" = "FAILED" ] && {
    aws athena get-query-execution --query-execution-id "$QID" --region "$REGION" \
      --query 'QueryExecution.Status.StateChangeReason'
    exit 1
  }
  sleep 5
done

aws athena get-query-results --query-execution-id "$QID" --region "$REGION" \
  --query 'ResultSet.Rows[].Data[].VarCharValue' --output table

echo "==> 5. checking the dead-letter queue is empty"
aws sqs get-queue-attributes --queue-url "$DLQ_URL" --region "$REGION" \
  --attribute-names ApproximateNumberOfMessages \
  --query 'Attributes.ApproximateNumberOfMessages' --output text

echo "==> smoke test complete"
