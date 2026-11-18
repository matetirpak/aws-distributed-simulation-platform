# Read-only role used by every plan: PR plans (untrusted code!), plans on main
# and drift detection. Plans always run with -lock=false, so no write access.
locals {
  plan_trust_policy = {
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GitHubActionsPlan"
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_host}:aud" = ["sts.amazonaws.com"]
          "${local.oidc_host}:sub" = [
            "${var.github_oidc_subject_prefix}:pull_request",
            "${var.github_oidc_subject_prefix}:ref:refs/heads/main",
          ]
          # Defence in depth: the immutable sub already embeds these IDs.
          "${local.oidc_host}:repository_id"       = [var.github_repository_id]
          "${local.oidc_host}:repository_owner_id" = [var.github_repository_owner_id]
        }
      }
    }]
  }

  plan_state_read_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListStateBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.state.arn
      },
      {
        Sid      = "ReadStateObjects"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.state.arn}/*"
      },
      {
        Sid      = "DecryptState"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey"]
        Resource = aws_kms_key.state.arn
      },
    ]
  }

  # PR plans execute code from any branch (data "external", malicious providers),
  # so the role must not be able to read secret material. If a legitimate plan
  # needs one of these, report it instead of widening the role.
  plan_deny_secret_material_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DenySecretValues"
        Effect   = "Deny"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]
        Resource = "*"
      },
      {
        Sid         = "DenyDecryptOutsideState"
        Effect      = "Deny"
        Action      = "kms:Decrypt"
        NotResource = aws_kms_key.state.arn
      },
      {
        Sid         = "DenyDataObjectReads"
        Effect      = "Deny"
        Action      = ["s3:GetObject", "s3:GetObjectVersion"]
        NotResource = "${aws_s3_bucket.state.arn}/*"
      },
    ]
  }
}

resource "aws_iam_role" "plan" {
  name                 = local.plan_role_name
  description          = "GitHub Actions: terraform plan (read-only). Managed by infra/bootstrap."
  assume_role_policy   = jsonencode(local.plan_trust_policy)
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_read_only" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "plan_state_read" {
  name   = "terraform-state-read"
  role   = aws_iam_role.plan.id
  policy = jsonencode(local.plan_state_read_policy)
}

resource "aws_iam_role_policy" "plan_deny_secret_material" {
  name   = "deny-secret-material"
  role   = aws_iam_role.plan.id
  policy = jsonencode(local.plan_deny_secret_material_policy)
}
