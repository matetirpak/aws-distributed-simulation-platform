data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region

  bucket_name = "${var.name_prefix}-tfstate-${local.account_id}-${local.region}"
  kms_alias   = "alias/${var.name_prefix}-tfstate"

  oidc_host         = "token.actions.githubusercontent.com"
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn

  plan_role_name  = "gha-terraform-plan"
  apply_role_name = "gha-terraform-apply"
  plan_role_arn   = "arn:${local.partition}:iam::${local.account_id}:role/${local.plan_role_name}"
  apply_role_arn  = "arn:${local.partition}:iam::${local.account_id}:role/${local.apply_role_name}"
}
