output "state_bucket_name" {
  description = "State bucket, for each root's backend.tf."
  value       = aws_s3_bucket.state.bucket
}

output "state_kms_key_arn" {
  description = "KMS key encrypting the state bucket."
  value       = aws_kms_key.state.arn
}

output "state_kms_alias" {
  description = "Alias to use as kms_key_id in backend.tf."
  value       = aws_kms_alias.state.name
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider."
  value       = local.oidc_provider_arn
}

output "plan_role_arn" {
  description = "For the INFRA_<ENV> repository variable (docs/ci-cd.md)."
  value       = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  description = "For the INFRA_<ENV> repository variable (docs/ci-cd.md)."
  value       = aws_iam_role.apply.arn
}

output "ci_json" {
  description = "Compare with infra/envs/<env>/ci.json (approver_ids, apply_after and allow_self_review are maintained by hand)."
  value = jsonencode({
    aws_region = local.region
    auto_apply = var.auto_apply
  })
}

output "ci_variable" {
  description = "The account part of the INFRA_<ENV> repository variable; add the root's tfvars (docs/ci-cd.md)."
  value = jsonencode({
    plan_role_arn  = aws_iam_role.plan.arn
    apply_role_arn = aws_iam_role.apply.arn
    state_bucket   = aws_s3_bucket.state.bucket
  })
}

output "plan_trust_policy" {
  description = "Trust policy of the plan role, for review."
  value       = local.plan_trust_policy
}

output "apply_trust_policy" {
  description = "Trust policy of the apply role, for review."
  value       = local.apply_trust_policy
}
