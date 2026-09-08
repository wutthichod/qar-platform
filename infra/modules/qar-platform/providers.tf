# Provider configuration lives in infra/envs/*, as a reusable module must
# not configure its own provider.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
