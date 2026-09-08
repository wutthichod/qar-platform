# ---------------------------------------------------------------------------
# SQS to Batch. The only glue in the pipeline.
# ---------------------------------------------------------------------------
data "archive_file" "submit_job" {
  type        = "zip"
  source_file = "${path.module}/lambda/submit_job.py"
  output_path = "${path.module}/build/submit_job.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "submit_job" {
  name               = "${var.project}-submit-job"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "submit_job" {
  name = "submit-job"
  role = aws_iam_role.submit_job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["batch:SubmitJob"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = aws_sqs_queue.decode.arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = aws_kms_key.data.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.submit_job.arn}:*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "submit_job" {
  name              = "/aws/lambda/${var.project}-submit-job"
  retention_in_days = 30
}

resource "aws_lambda_function" "submit_job" {
  function_name    = "${var.project}-submit-job"
  role             = aws_iam_role.submit_job.arn
  handler          = "submit_job.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.submit_job.output_path
  source_code_hash = data.archive_file.submit_job.output_base64sha256
  timeout          = 60

  environment {
    variables = {
      JOB_QUEUE      = aws_batch_job_queue.decode.name
      JOB_DEFINITION = aws_batch_job_definition.decode.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.submit_job]
}

resource "aws_lambda_event_source_mapping" "decode_queue" {
  event_source_arn = aws_sqs_queue.decode.arn
  function_name    = aws_lambda_function.submit_job.arn
  batch_size       = 10
}
