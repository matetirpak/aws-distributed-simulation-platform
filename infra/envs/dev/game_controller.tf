# The game controller image. The game's own repository builds it and pushes release tags here;
# only digests pinned after review run (docs/architecture.md).
module "game_controller_ecr" {
  source = "../../modules/ecr"
  name   = "${local.name_prefix}/game-controller"
}

# Lets the game repository's release workflow push, and nothing else:
#   - only release.yml, only when it runs for a version tag (v*), only that repository
#   - only into the repository above; no deletes, no other AWS access
# Who may create v* tags is up to the game repository's own rulesets. The workflow refuses tags whose
# commit isn't on main, and nothing runs until we pin the image's digest.
locals {
  oidc_host = "token.actions.githubusercontent.com"
  # The GitHub OIDC provider the bootstrap creates. Built from the account ID instead of
  # looked up, so an account without the bootstrap can still plan this root.
  github_oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/${local.oidc_host}"
  game_server              = var.game_server
}

resource "aws_iam_role" "game_server_ecr_push" {
  name                 = "${local.name_prefix}-game-ecr-push"
  description          = "GitHub Actions: the game repository's release.yml on tags v* pushes the game-controller image. Managed by Terraform."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ReleaseWorkflowOnVersionTags"
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_host}:aud"                 = ["sts.amazonaws.com"]
          "${local.oidc_host}:repository_id"       = [local.game_server.id]
          "${local.oidc_host}:repository_owner_id" = [local.game_server.owner_id]
        }
        StringLike = {
          "${local.oidc_host}:sub"              = ["${local.game_server.subject_prefix}:ref:refs/tags/v*"]
          "${local.oidc_host}:job_workflow_ref" = ["${local.game_server.repository}/.github/workflows/release.yml@refs/tags/v*"]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "game_server_ecr_push" {
  name = "ecr-push"
  role = aws_iam_role.game_server_ecr_push.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Registry login; AWS offers no narrower resource for this action.
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "PushToOneRepository"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = module.game_controller_ecr.arn
      },
    ]
  })
}
