# ---------------------------------------------------------------------------
# Athena workgroup. The scan limit is the guardrail that stops one
# unpartitioned SELECT * from becoming a budget incident.
# ---------------------------------------------------------------------------
resource "aws_athena_workgroup" "analysts" {
  name        = "${var.project}-analysts"
  description = "FOQA analyst queries. Scan-capped and audited."

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.athena_scan_limit_bytes

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.id}/analysts/"

      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = aws_kms_key.data.arn
      }
    }
  }
}

# ---------------------------------------------------------------------------
# The analyst role.
#
# Note what is absent: no s3:GetObject on the lakehouse bucket. Analysts
# reach data only through Athena, which is what makes Lake Formation's
# column filtering an actual boundary rather than a convention.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "analyst_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type = "AWS"
      identifiers = length(var.analyst_principal_arns) > 0 ? var.analyst_principal_arns : [
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      ]
    }
  }
}

resource "aws_iam_role" "analyst" {
  name               = "${var.project}-analyst"
  assume_role_policy = data.aws_iam_policy_document.analyst_assume.json
}

data "aws_iam_policy_document" "analyst" {
  statement {
    sid    = "RunQueries"
    effect = "Allow"
    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
      "athena:GetWorkGroup",
      "athena:ListQueryExecutions",
    ]
    resources = [aws_athena_workgroup.analysts.arn]
  }

  statement {
    sid    = "ReadCatalogThroughLakeFormation"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
      "glue:GetPartition",
      "glue:GetPartitions",
      "lakeformation:GetDataAccess",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "OwnQueryResults"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.athena_results.arn,
      "${aws_s3_bucket.athena_results.arn}/*",
    ]
  }

  statement {
    sid    = "ReadGovernedExtracts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.extracts.arn,
      "${aws_s3_bucket.extracts.arn}/*",
    ]
  }

  statement {
    sid       = "DecryptResults"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "analyst" {
  name   = "analyst"
  role   = aws_iam_role.analyst.id
  policy = data.aws_iam_policy_document.analyst.json
}

# Deny lakehouse object access outright, so a future policy edit cannot
# quietly open the hole that FR-8 depends on staying closed.
resource "aws_iam_role_policy" "analyst_deny_lakehouse" {
  name = "deny-lakehouse-objects"
  role = aws_iam_role.analyst.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Deny"
      Action = ["s3:GetObject", "s3:ListBucket", "s3:GetObjectVersion"]
      Resource = [
        aws_s3_bucket.lakehouse.arn,
        "${aws_s3_bucket.lakehouse.arn}/*",
        aws_s3_bucket.raw_archive.arn,
        "${aws_s3_bucket.raw_archive.arn}/*",
      ]
    }]
  })
}
