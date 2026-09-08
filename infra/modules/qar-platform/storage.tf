# ---------------------------------------------------------------------------
# Encryption key shared by the data buckets.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "data" {
  description             = "${var.project} lakehouse encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "data" {
  name          = "alias/${var.project}-data"
  target_key_id = aws_kms_key.data.key_id
}

# ---------------------------------------------------------------------------
# Landing zone. Files arrive here and are moved on. Short retention.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "landing" {
  bucket = "${var.project}-landing-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "landing" {
  bucket                  = aws_s3_bucket.landing.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

# ---------------------------------------------------------------------------
# Raw archive. Immutable. This is the layer that makes re-decode possible,
# so it is versioned and under Object Lock.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "raw_archive" {
  bucket              = "${var.project}-raw-archive-${data.aws_caller_identity.current.account_id}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.raw_archive_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.raw_archive]
}

resource "aws_s3_bucket_public_access_block" "raw_archive" {
  bucket                  = aws_s3_bucket.raw_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  rule {
    id     = "tier-cold"
    status = "Enabled"
    filter {}
    transition {
      days          = 90
      storage_class = "INTELLIGENT_TIERING"
    }
  }
}

# ---------------------------------------------------------------------------
# Lakehouse. Iceberg tables live here. No analyst ever gets s3:GetObject
# on this bucket - the query engine is the only credentialed path.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "lakehouse" {
  bucket = "${var.project}-lakehouse-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "lakehouse" {
  bucket = aws_s3_bucket.lakehouse.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "lakehouse" {
  bucket                  = aws_s3_bucket.lakehouse.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lakehouse" {
  bucket = aws_s3_bucket.lakehouse.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

# ---------------------------------------------------------------------------
# Governed extracts. Filtered output analysts may pull into DuckDB.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "extracts" {
  bucket = "${var.project}-extracts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "extracts" {
  bucket                  = aws_s3_bucket.extracts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "extracts" {
  bucket = aws_s3_bucket.extracts.id
  rule {
    id     = "expire-extracts"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

# ---------------------------------------------------------------------------
# Athena query results.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "athena_results" {
  bucket = "${var.project}-athena-results-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "athena_results" {
  bucket                  = aws_s3_bucket.athena_results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id
  rule {
    id     = "expire-results"
    status = "Enabled"
    filter {}
    expiration {
      days = 14
    }
  }
}
