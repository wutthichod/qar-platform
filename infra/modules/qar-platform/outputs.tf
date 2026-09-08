output "landing_bucket" {
  description = "Drop QAR files here."
  value       = aws_s3_bucket.landing.id
}

output "raw_archive_bucket" {
  value = aws_s3_bucket.raw_archive.id
}

output "lakehouse_bucket" {
  value = aws_s3_bucket.lakehouse.id
}

output "extracts_bucket" {
  value = aws_s3_bucket.extracts.id
}

output "decoder_repository_url" {
  description = "Push the decode image here before submitting any jobs."
  value       = aws_ecr_repository.decoder.repository_url
}

output "decode_queue_url" {
  value = aws_sqs_queue.decode.url
}

output "decode_dlq_url" {
  description = "Failed files land here. This is the quarantine list."
  value       = aws_sqs_queue.decode_dlq.url
}

output "batch_job_queue" {
  value = aws_batch_job_queue.decode.name
}

output "batch_backfill_queue" {
  description = "Low priority queue for re-decodes and sweeps."
  value       = aws_batch_job_queue.backfill.name
}

output "batch_job_definition" {
  value = aws_batch_job_definition.decode.name
}

output "athena_workgroup" {
  value = aws_athena_workgroup.analysts.name
}

output "analyst_role_arn" {
  value = aws_iam_role.analyst.arn
}

output "glue_databases" {
  value = {
    bronze = aws_glue_catalog_database.bronze.name
    silver = aws_glue_catalog_database.silver.name
    gold   = aws_glue_catalog_database.gold.name
  }
}
