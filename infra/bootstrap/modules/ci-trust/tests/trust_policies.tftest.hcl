# Offline tests for the security-critical trust policies (no AWS access needed).
# Run: cd infra/bootstrap/modules/ci-trust && terraform init -backend=false -lockfile=readonly && terraform test

provider "aws" {
  region                      = "eu-central-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}

override_data {
  target          = data.aws_caller_identity.current
  override_during = plan
  values          = { account_id = "111111111111" }
}

override_data {
  target          = data.aws_partition.current
  override_during = plan
  values          = { partition = "aws" }
}

override_data {
  target          = data.aws_region.current
  override_during = plan
  values          = { region = "eu-central-1" }
}

override_resource {
  target          = aws_s3_bucket.state
  override_during = plan
  values          = { arn = "arn:aws:s3:::test-tfstate-111111111111-eu-central-1", id = "test-tfstate-111111111111-eu-central-1" }
}

override_resource {
  target          = aws_kms_key.state
  override_during = plan
  values          = { arn = "arn:aws:kms:eu-central-1:111111111111:key/00000000-0000-0000-0000-000000000000", key_id = "00000000-0000-0000-0000-000000000000" }
}

override_resource {
  target          = aws_iam_openid_connect_provider.github
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com" }
}

variables {
  name_prefix                = "test"
  environment                = "dev"
  github_repository          = "example-owner/example-repo"
  github_repository_id       = "1000001"
  github_repository_owner_id = "2000002"
  github_oidc_subject_prefix = "repo:example-owner@2000002/example-repo@1000001"
  approval_mode              = "degraded"
  approver_actor_ids         = ["3000003"]
}

run "plan_role_trusts_only_pr_and_main" {
  command = plan

  assert {
    condition     = keys(local.plan_trust_policy.Statement[0].Condition) == ["StringEquals"]
    error_message = "Plan role trust must use StringEquals only (no wildcards)."
  }

  assert {
    condition     = toset(local.plan_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset(["repo:example-owner@2000002/example-repo@1000001:pull_request", "repo:example-owner@2000002/example-repo@1000001:ref:refs/heads/main"])
    error_message = "Plan role must trust exactly the pull_request and main subjects."
  }

  assert {
    condition     = toset([for s in local.plan_deny_secret_material_policy.Statement : s.Sid if s.Effect == "Deny"]) == toset(["DenySecretValues", "DenyDecryptOutsideState", "DenyDataObjectReads"])
    error_message = "Plan role must deny reading secret material."
  }
}

run "state_bucket_and_trust_anchors_are_protected" {
  command = plan

  assert {
    condition     = toset([for s in local.state_bucket_policy.Statement : s.Sid]) == toset(["DenyInsecureTransport", "DenyNonKmsUploads"])
    error_message = "State bucket policy must deny non-TLS access and non-KMS uploads."
  }

  assert {
    condition     = toset([for s in local.apply_protect_trust_anchors_policy.Statement : s.Sid if s.Effect == "Deny"]) == toset(["ProtectCiRolesAndOidcProvider", "ProtectStateBucketConfiguration", "ProtectStateKey", "ProtectBootstrapState"])
    error_message = "Apply role must be denied from modifying the CI trust anchors."
  }
}

run "degraded_apply_trusts_apply_workflow_on_main_and_approvers_only" {
  command = plan

  assert {
    condition     = keys(local.apply_trust_policy.Statement[0].Condition) == ["StringEquals"]
    error_message = "Apply role trust must use StringEquals only."
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset(["repo:example-owner@2000002/example-repo@1000001:ref:refs/heads/main"])
    error_message = "Degraded mode: apply role must trust only refs/heads/main."
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:job_workflow_ref"]) == toset(["example-owner/example-repo/.github/workflows/apply.yml@refs/heads/main"])
    error_message = "Degraded mode: apply role must be restricted to apply.yml on main."
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:actor_id"]) == toset(["3000003"])
    error_message = "Degraded mode: apply role must be restricted to the approvers."
  }
}

run "auto_apply_is_off_by_default" {
  command = plan

  assert {
    condition     = length(local.apply_trust_policy.Statement) == 1
    error_message = "Without auto_apply the apply role must have exactly one trust statement."
  }
}

run "auto_apply_trusts_the_deploy_workflow_on_main" {
  command = plan

  variables {
    auto_apply = true
  }

  assert {
    condition     = length(local.apply_trust_policy.Statement) == 2 && local.apply_trust_policy.Statement[1].Sid == "GitHubActionsAutoApplyAfterMerge"
    error_message = "auto_apply must add exactly one statement for the deploy run."
  }

  assert {
    condition     = keys(local.apply_trust_policy.Statement[1].Condition) == ["StringEquals"]
    error_message = "The auto-apply statement must use StringEquals only."
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset(["repo:example-owner@2000002/example-repo@1000001:ref:refs/heads/main"])
    error_message = "Auto-apply: the apply role must trust only runs on refs/heads/main."
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:job_workflow_ref"]) == toset(["example-owner/example-repo/.github/workflows/_terraform-env.yml@refs/heads/main"])
    error_message = "Auto-apply: the apply role must be restricted to _terraform-env.yml on main."
  }

  assert {
    condition     = alltrue([for k in ["repository_id", "repository_owner_id", "aud"] : contains(keys(local.apply_trust_policy.Statement[1].Condition.StringEquals), "token.actions.githubusercontent.com:${k}")])
    error_message = "Auto-apply: the repository, owner and audience checks must stay in place."
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:actor_id"]) == toset(["3000003"])
    error_message = "Auto-apply must not loosen the approver statement."
  }
}

run "auto_apply_requires_degraded_mode" {
  command = plan

  variables {
    approval_mode = "B"
    auto_apply    = true
  }

  expect_failures = [aws_iam_role.apply]
}

run "mode_b_apply_trusts_environment_workflow_and_approvers" {
  command = plan

  variables {
    approval_mode = "B"
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset(["repo:example-owner@2000002/example-repo@1000001:environment:dev"])
    error_message = "Mode B: apply role must trust the environment subject."
  }

  assert {
    condition     = alltrue([for k in ["job_workflow_ref", "actor_id"] : contains(keys(local.apply_trust_policy.Statement[0].Condition.StringEquals), "token.actions.githubusercontent.com:${k}")])
    error_message = "Mode B: workflow and approver checks must stay in place."
  }
}

run "mode_a_apply_trusts_exactly_the_environment" {
  command = plan

  variables {
    approval_mode = "A"
  }

  assert {
    condition     = toset(local.apply_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset(["repo:example-owner@2000002/example-repo@1000001:environment:dev"])
    error_message = "Mode A: apply role must trust exactly environment:dev."
  }

  assert {
    condition     = !anytrue([for k in ["job_workflow_ref", "actor_id"] : contains(keys(local.apply_trust_policy.Statement[0].Condition.StringEquals), "token.actions.githubusercontent.com:${k}")])
    error_message = "Mode A: environment reviewers are the gate; no workflow/actor conditions."
  }
}

run "rejects_unknown_mode" {
  command = plan

  variables {
    approval_mode = "C"
  }

  expect_failures = [var.approval_mode]
}

run "rejects_wildcard_subject_prefix" {
  command = plan

  variables {
    github_oidc_subject_prefix = "repo:example-owner/*"
  }

  expect_failures = [var.github_oidc_subject_prefix]
}

run "degraded_requires_an_approver" {
  command = plan

  variables {
    approver_actor_ids = []
  }

  expect_failures = [aws_iam_role.apply]
}
