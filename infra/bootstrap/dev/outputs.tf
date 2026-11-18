output "state_bucket_name" {
  description = "State bucket, for each root's backend.tf."
  value       = module.ci_trust.state_bucket_name
}

output "state_kms_alias" {
  description = "kms_key_id for backend.tf."
  value       = module.ci_trust.state_kms_alias
}

output "plan_role_arn" {
  description = "For the INFRA_DEV repository variable."
  value       = module.ci_trust.plan_role_arn
}

output "apply_role_arn" {
  description = "For the INFRA_DEV repository variable."
  value       = module.ci_trust.apply_role_arn
}

output "ci_json" {
  description = "Compare with infra/envs/dev/ci.json."
  value       = module.ci_trust.ci_json
}

output "ci_variable" {
  description = "The account part of the INFRA_DEV repository variable; add tfvars (docs/ci-cd.md)."
  value       = module.ci_trust.ci_variable
}
