provider "aws" {
  region              = local.aws_region
  allowed_account_ids = [local.account_id]

  default_tags {
    tags = {
      Environment = local.environment
      ManagedBy   = "terraform"
      Repository  = var.github_repository
      Root        = "infra/bootstrap/${local.environment}"
    }
  }
}
