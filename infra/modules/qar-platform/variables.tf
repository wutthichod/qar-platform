variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "qar"
}

variable "region" {
  description = "AWS region. Check data residency requirements before changing."
  type        = string
  default     = "ap-southeast-1"
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "subnet_ids" {
  description = <<-EOT
    Subnets for Batch Fargate tasks. Private subnets with a NAT gateway or
    VPC endpoints for ECR, S3 and CloudWatch Logs are the production shape.
    Public subnets work for a first deployment if assign_public_ip is true.
  EOT
  type        = list(string)
}

variable "vpc_id" {
  description = "VPC containing the subnets above."
  type        = string
}

variable "assign_public_ip" {
  description = "Set false once tasks run in private subnets with egress."
  type        = bool
  default     = true
}

variable "raw_archive_retention_days" {
  description = "Object Lock retention on raw QAR files. Check the FOQA agreement."
  type        = number
  default     = 3650
}

variable "decode_vcpu" {
  description = "vCPU per decode task."
  type        = string
  default     = "1"
}

variable "decode_memory_mb" {
  description = "Memory per decode task in MiB."
  type        = string
  default     = "2048"
}

variable "batch_max_vcpus" {
  description = "Ceiling on concurrent decode capacity. This is your cost cap."
  type        = number
  default     = 64
}

variable "job_retry_attempts" {
  description = "Batch retry attempts before a job is considered failed."
  type        = number
  default     = 3
}

variable "athena_scan_limit_bytes" {
  description = "Per-query scan ceiling for the analyst workgroup. Default 100 GB."
  type        = number
  default     = 107374182400
}

variable "analyst_principal_arns" {
  description = "Principals allowed to assume the analyst role."
  type        = list(string)
  default     = []
}

variable "restricted_columns" {
  description = "Columns hidden from analysts by Lake Formation. FR-8."
  type        = list(string)
  default     = ["crew_id", "captain_name", "first_officer_name"]
}

variable "silver_flights_table" {
  description = <<-EOT
    Name of the Silver table to apply column-level permissions to.
    Leave empty until the decoder has created it; Lake Formation cannot
    grant on a table that does not exist yet.
  EOT
  type        = string
  default     = ""
}
