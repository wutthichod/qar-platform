# ---------------------------------------------------------------------------
# Container registry for the decode image.
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "decoder" {
  name                 = "${var.project}-decoder"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
}

resource "aws_ecr_lifecycle_policy" "decoder" {
  repository = aws_ecr_repository.decoder.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThanImagesNumber"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# Networking for Fargate tasks.
# ---------------------------------------------------------------------------
resource "aws_security_group" "batch" {
  name        = "${var.project}-batch"
  description = "Decode tasks. Egress only."
  vpc_id      = var.vpc_id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------------------
# IAM. Two roles: the execution role pulls the image and writes logs;
# the job role is what your decoder code actually uses.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_execution" {
  name               = "${var.project}-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "batch_execution" {
  role       = aws_iam_role.batch_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "batch_execution_kms" {
  name = "kms-decrypt"
  role = aws_iam_role.batch_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
      Resource = [aws_kms_key.data.arn]
    }]
  })
}

resource "aws_iam_role" "decode_job" {
  name               = "${var.project}-decode-job"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "decode_job" {
  statement {
    sid    = "ReadLandingAndArchive"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.landing.arn,
      "${aws_s3_bucket.landing.arn}/*",
      aws_s3_bucket.raw_archive.arn,
      "${aws_s3_bucket.raw_archive.arn}/*",
    ]
  }

  statement {
    sid       = "ArchiveRawFiles"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw_archive.arn}/*"]
  }

  statement {
    sid    = "WriteLakehouse"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:AbortMultipartUpload",
    ]
    resources = [
      aws_s3_bucket.lakehouse.arn,
      "${aws_s3_bucket.lakehouse.arn}/*",
    ]
  }

  statement {
    sid    = "IcebergCatalogCommits"
    effect = "Allow"
    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:GetTable",
      "glue:GetTables",
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:GetPartition",
      "glue:GetPartitions",
      "glue:BatchCreatePartition",
      "glue:UpdatePartition",
    ]
    resources = [
      "arn:aws:glue:${var.region}:${data.aws_caller_identity.current.account_id}:catalog",
      "arn:aws:glue:${var.region}:${data.aws_caller_identity.current.account_id}:database/${var.project}_*",
      "arn:aws:glue:${var.region}:${data.aws_caller_identity.current.account_id}:table/${var.project}_*/*",
    ]
  }

  statement {
    sid       = "ConsumeQueue"
    effect    = "Allow"
    actions   = ["sqs:DeleteMessage", "sqs:ReceiveMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.decode.arn]
  }

  statement {
    sid       = "UseDataKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_role_policy" "decode_job" {
  name   = "decode-job"
  role   = aws_iam_role.decode_job.id
  policy = data.aws_iam_policy_document.decode_job.json
}

# ---------------------------------------------------------------------------
# Batch. Fargate to start; switch to EC2 Spot when volume justifies it.
# ---------------------------------------------------------------------------
resource "aws_batch_compute_environment" "decode" {
  name  = "${var.project}-decode"
  type  = "MANAGED"
  state = "ENABLED"

  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.batch_max_vcpus
    subnets            = var.subnet_ids
    security_group_ids = [aws_security_group.batch.id]
  }
}

resource "aws_batch_job_queue" "decode" {
  name     = "${var.project}-decode"
  state    = "ENABLED"
  priority = 100

  compute_environment_order {
    order               = 0
    compute_environment = aws_batch_compute_environment.decode.arn
  }
}

# Lower priority queue for backfills, so a six-month re-decode never
# delays today's arrivals.
resource "aws_batch_job_queue" "backfill" {
  name     = "${var.project}-backfill"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 0
    compute_environment = aws_batch_compute_environment.decode.arn
  }
}

resource "aws_cloudwatch_log_group" "decode" {
  name              = "/aws/batch/${var.project}-decode"
  retention_in_days = 30
}

resource "aws_batch_job_definition" "decode" {
  name                  = "${var.project}-decode"
  type                  = "container"
  platform_capabilities = ["FARGATE"]

  retry_strategy {
    attempts = var.job_retry_attempts
  }

  timeout {
    attempt_duration_seconds = 3600
  }

  container_properties = jsonencode({
    image            = "${aws_ecr_repository.decoder.repository_url}:latest"
    jobRoleArn       = aws_iam_role.decode_job.arn
    executionRoleArn = aws_iam_role.batch_execution.arn

    resourceRequirements = [
      { type = "VCPU", value = var.decode_vcpu },
      { type = "MEMORY", value = var.decode_memory_mb },
    ]

    fargatePlatformConfiguration = {
      platformVersion = "LATEST"
    }

    networkConfiguration = {
      assignPublicIp = var.assign_public_ip ? "ENABLED" : "DISABLED"
    }

    environment = [
      { name = "RAW_ARCHIVE_BUCKET", value = aws_s3_bucket.raw_archive.id },
      { name = "LAKEHOUSE_BUCKET", value = aws_s3_bucket.lakehouse.id },
      { name = "GLUE_DATABASE", value = aws_glue_catalog_database.silver.name },
      { name = "AWS_REGION", value = var.region },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.decode.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "decode"
      }
    }
  })
}
