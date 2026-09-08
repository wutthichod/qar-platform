# Dev environment. Calls the shared module with dev-specific values.
# State lives per environment so a dev apply can never reconcile prod.

terraform {
  required_version = ">= 1.8.0"

  # Create this bucket by hand once, before the first init.
  # backend "s3" {
  #   bucket       = "qar-tfstate-<account-id>"
  #   key          = "dev/terraform.tfstate"
  #   region       = "ap-southeast-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

module "platform" {
  source = "../../modules/qar-platform"

  project             = "qar-dev"
  region              = var.region
  vpc_id              = var.vpc_id
  subnet_ids          = var.subnet_ids

  assign_public_ip = true
  batch_max_vcpus  = 16

  tags = {
    Environment = "dev"
  }
}
