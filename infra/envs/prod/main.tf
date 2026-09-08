# Prod environment. Calls the shared module with dev-specific values.
# State lives per environment so a dev apply can never reconcile prod.

terraform {
  required_version = ">= 1.8.0"

  # Create this bucket by hand once, before the first init.
  # backend "s3" {
  #   bucket       = "qar-tfstate-<account-id>"
  #   key          = "prod/terraform.tfstate"
  #   region       = "ap-southeast-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

module "platform" {
  source = "../../modules/qar-platform"

  project             = "qar-prod"
  region              = var.region
  vpc_id              = var.vpc_id
  subnet_ids          = var.subnet_ids

  assign_public_ip = false
  batch_max_vcpus  = 64

  tags = {
    Environment = "prod"
  }
}
