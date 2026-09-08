"""Glue between SQS and AWS Batch.

Deliberately thin: it translates a queue message into a SubmitJob call and
nothing else. All decode logic lives in the container, so the pipeline stays
substrate-agnostic and this function never needs to change.
"""

import json
import os
import urllib.parse

import boto3

batch = boto3.client("batch")

JOB_QUEUE = os.environ["JOB_QUEUE"]
JOB_DEFINITION = os.environ["JOB_DEFINITION"]


def _job_name(key: str) -> str:
    """Batch job names allow only alphanumerics, hyphen and underscore."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in key)
    return f"decode-{safe}"[:128]


def handler(event, _context):
    submitted = []

    for record in event.get("Records", []):
        body = json.loads(record["body"])

        # S3 test events have no Records key.
        for s3_record in body.get("Records", []):
            bucket = s3_record["s3"]["bucket"]["name"]
            key = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])

            response = batch.submit_job(
                jobName=_job_name(key),
                jobQueue=JOB_QUEUE,
                jobDefinition=JOB_DEFINITION,
                containerOverrides={
                    "environment": [
                        {"name": "SOURCE_BUCKET", "value": bucket},
                        {"name": "SOURCE_KEY", "value": key},
                    ]
                },
            )
            submitted.append(response["jobId"])

    return {"submitted": submitted}
