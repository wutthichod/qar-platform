# ---------------------------------------------------------------------------
# Work queue. One message per arriving file. The DLQ is where the
# quarantine list actually comes from.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "decode_dlq" {
  name                      = "${var.project}-decode-dlq"
  message_retention_seconds = 1209600 # 14 days
  kms_master_key_id         = aws_kms_key.data.id
}

resource "aws_sqs_queue" "decode" {
  name                       = "${var.project}-decode"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 345600
  kms_master_key_id          = aws_kms_key.data.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.decode_dlq.arn
    maxReceiveCount     = 3
  })
}

data "aws_iam_policy_document" "decode_queue" {
  statement {
    sid    = "AllowS3Notification"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.decode.arn]
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.landing.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "decode" {
  queue_url = aws_sqs_queue.decode.id
  policy    = data.aws_iam_policy_document.decode_queue.json
}

resource "aws_s3_bucket_notification" "landing" {
  bucket = aws_s3_bucket.landing.id

  queue {
    queue_arn = aws_sqs_queue.decode.arn
    events    = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_sqs_queue_policy.decode]
}
