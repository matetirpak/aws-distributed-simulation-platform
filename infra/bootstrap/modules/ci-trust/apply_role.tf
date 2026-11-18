# Role that applies saved plans. Who can assume it depends on approval_mode:
#
#   degraded  sub = <prefix>:ref:refs/heads/main
#             AND job_workflow_ref = <repo>/.github/workflows/apply.yml@refs/heads/main
#             AND actor_id in approver_actor_ids
#   B         sub = <prefix>:environment:<env>   (+ the same workflow and actor checks)
#   A         sub = <prefix>:environment:<env>   (environment reviewers are the gate)
#
# With auto_apply (degraded only), a second statement trusts the deploy run after a merge:
#             sub = <prefix>:ref:refs/heads/main
#             AND job_workflow_ref = <repo>/.github/workflows/_terraform-env.yml@refs/heads/main
# _terraform-env.yml is callable on purpose; a branch calling it at @main still fails the
# sub condition, so only workflows running on main (reviewed code) can present both.
#
# apply.yml must stay workflow_dispatch-only: if it were callable (workflow_call),
# a branch could call it at @main and present a matching job_workflow_ref.
locals {
  apply_subject      = var.approval_mode == "degraded" ? "${var.github_oidc_subject_prefix}:ref:refs/heads/main" : "${var.github_oidc_subject_prefix}:environment:${var.environment}"
  apply_workflow_ref = "${var.github_repository}/.github/workflows/apply.yml@refs/heads/main"
  apply_gated_by_ids = var.approval_mode != "A"
  auto_workflow_ref  = "${var.github_repository}/.github/workflows/_terraform-env.yml@refs/heads/main"

  apply_trust_policy = {
    Version = "2012-10-17"
    Statement = concat([{
      Sid       = "GitHubActionsApply"
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = merge(
          {
            "${local.oidc_host}:aud"                 = ["sts.amazonaws.com"]
            "${local.oidc_host}:sub"                 = [local.apply_subject]
            "${local.oidc_host}:repository_id"       = [var.github_repository_id]
            "${local.oidc_host}:repository_owner_id" = [var.github_repository_owner_id]
          },
          local.apply_gated_by_ids ? {
            "${local.oidc_host}:job_workflow_ref" = [local.apply_workflow_ref]
            "${local.oidc_host}:actor_id"         = var.approver_actor_ids
          } : {},
        )
      }
      }], var.auto_apply ? [{
      Sid       = "GitHubActionsAutoApplyAfterMerge"
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_host}:aud"                 = ["sts.amazonaws.com"]
          "${local.oidc_host}:sub"                 = ["${var.github_oidc_subject_prefix}:ref:refs/heads/main"]
          "${local.oidc_host}:repository_id"       = [var.github_repository_id]
          "${local.oidc_host}:repository_owner_id" = [var.github_repository_owner_id]
          "${local.oidc_host}:job_workflow_ref"    = [local.auto_workflow_ref]
        }
      }
    }] : [])
  }

  # Terraform run from CI must never be able to loosen the controls that gate CI.
  apply_protect_trust_anchors_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ProtectCiRolesAndOidcProvider"
        Effect = "Deny"
        Action = [
          "iam:Add*", "iam:Attach*", "iam:Create*", "iam:Delete*", "iam:Detach*",
          "iam:Put*", "iam:Remove*", "iam:Set*", "iam:Tag*", "iam:Untag*", "iam:Update*",
        ]
        Resource = [local.oidc_provider_arn, local.plan_role_arn, local.apply_role_arn]
      },
      {
        Sid    = "ProtectStateBucketConfiguration"
        Effect = "Deny"
        Action = [
          "s3:DeleteBucket", "s3:DeleteBucketPolicy", "s3:PutBucketPolicy", "s3:PutBucketAcl",
          "s3:PutBucketOwnershipControls", "s3:PutBucketPublicAccessBlock", "s3:PutBucketVersioning",
          "s3:PutEncryptionConfiguration", "s3:PutLifecycleConfiguration", "s3:PutBucketObjectLockConfiguration",
        ]
        Resource = aws_s3_bucket.state.arn
      },
      {
        Sid    = "ProtectStateKey"
        Effect = "Deny"
        Action = [
          "kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:DisableKey", "kms:DisableKeyRotation",
          "kms:UpdateAlias", "kms:DeleteAlias",
        ]
        Resource = [aws_kms_key.state.arn, "arn:${local.partition}:kms:${local.region}:${local.account_id}:${local.kms_alias}"]
      },
      {
        # The bootstrap's own state lives under bootstrap/; CI must not be able to touch it.
        Sid      = "ProtectBootstrapState"
        Effect   = "Deny"
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource = "${aws_s3_bucket.state.arn}/bootstrap/*"
      },
    ]
  }
}

resource "aws_iam_role" "apply" {
  name                 = local.apply_role_name
  description          = "GitHub Actions: terraform apply of an approved saved plan (${var.approval_mode} mode). Managed by infra/bootstrap."
  assume_role_policy   = jsonencode(local.apply_trust_policy)
  max_session_duration = 3600

  lifecycle {
    precondition {
      condition     = !local.apply_gated_by_ids || length(var.approver_actor_ids) > 0
      error_message = "approval_mode ${var.approval_mode} needs at least one approver_actor_id."
    }
    precondition {
      condition     = !var.auto_apply || var.approval_mode == "degraded"
      error_message = "auto_apply is only for approval_mode degraded (modes A and B gate applies through an environment)."
    }
  }
}

# Starting point for dev. Scoping this down is a tracked follow-up (docs/ci-cd.md).
resource "aws_iam_role_policy_attachment" "apply_admin" {
  role       = aws_iam_role.apply.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AdministratorAccess"
}

resource "aws_iam_role_policy" "apply_protect_trust_anchors" {
  name   = "protect-ci-trust-anchors"
  role   = aws_iam_role.apply.id
  policy = jsonencode(local.apply_protect_trust_anchors_policy)
}
