provider "aws" {
  region = local.aws_region

  # Refuse to run against any other account, whatever credentials are active.
  allowed_account_ids = [local.account_id]

  default_tags {
    tags = {
      Environment = local.environment
      ManagedBy   = "terraform"
      Repository  = var.repository
      Root        = "infra/envs/${local.environment}"
    }
  }
}
