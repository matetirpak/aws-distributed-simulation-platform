module "ci_trust" {
  source = "../modules/ci-trust"

  name_prefix                = local.name_prefix
  environment                = local.environment
  github_repository          = var.github_repository
  github_repository_id       = var.github_repository_id
  github_repository_owner_id = var.github_repository_owner_id
  github_oidc_subject_prefix = var.github_oidc_subject_prefix
  approval_mode              = local.approval_mode
  approver_actor_ids         = var.approver_actor_ids
  auto_apply                 = local.auto_apply
}
