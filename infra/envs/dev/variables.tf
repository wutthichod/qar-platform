variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "allowed_account_ids" {
  description = "Guard: tofu refuses to run against any other account."
  type        = list(string)
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}
