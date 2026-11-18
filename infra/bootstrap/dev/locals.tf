# Values that aren't account details (those are inputs: variables.tf).
locals {
  environment = "dev"
  aws_region  = "eu-central-1"
  account_id  = var.account_id
  name_prefix = "dsim"

  # No environment reviewers for private repos below GitHub Enterprise, so the gate stays
  # in IAM. See docs/ci-cd.md.
  approval_mode = "degraded"
  # The deploy run after a merge applies plans without destroy, replace or forget
  # (infra/envs/dev/ci.json "auto_apply"); everything else waits for an approver.
  auto_apply = true
}
