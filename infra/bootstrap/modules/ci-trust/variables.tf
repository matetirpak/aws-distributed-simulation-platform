variable "name_prefix" {
  description = "Prefix for the state bucket and KMS alias, e.g. dsim."
  type        = string
}

variable "environment" {
  description = "Environment this account serves (dev, prod). Used in the apply role's trust in approval modes A and B."
  type        = string
}

variable "github_repository" {
  description = "OWNER/REPO exactly as GitHub reports it (case-sensitive); used for job_workflow_ref."
  type        = string
}

variable "github_repository_id" {
  description = "Immutable numeric repository ID."
  type        = string
}

variable "github_repository_owner_id" {
  description = "Immutable numeric owner (organization) ID."
  type        = string
}

variable "github_oidc_subject_prefix" {
  description = "Exact `sub` prefix from `gh api repos/OWNER/REPO/actions/oidc/customization/sub` (sub_claim_prefix)."
  type        = string

  validation {
    condition     = startswith(var.github_oidc_subject_prefix, "repo:") && !strcontains(var.github_oidc_subject_prefix, "*")
    error_message = "Must be an exact repo:... prefix without wildcards."
  }
}

variable "approval_mode" {
  description = <<-EOT
    How applies are approved (see docs/ci-cd.md):
      degraded - no environment reviewers (private repo below Enterprise). Apply role trusts only
                 apply.yml on main, dispatched by one of approver_actor_ids.
      B        - adds an environment (GitHub Team/Pro): apply.yml with `environment: <env>`
                 (main-only branch policy) + approver_actor_ids.
      A        - public repo or Enterprise: environment with required reviewers; apply runs from deploy.yml.
  EOT
  type        = string

  validation {
    condition     = contains(["degraded", "B", "A"], var.approval_mode)
    error_message = "approval_mode must be one of: degraded, B, A."
  }
}

variable "auto_apply" {
  description = <<-EOT
    Degraded mode only: also let the deploy run on main apply its own plan after a merge. The
    apply role then also trusts .github/workflows/_terraform-env.yml at refs/heads/main, for
    any actor. That workflow applies only plans without destroy, replace or forget, and only
    after the merge gate; the rest still waits for an approver (docs/ci-cd.md).
  EOT
  type        = bool
  default     = false
}

variable "approver_actor_ids" {
  description = "Immutable GitHub user IDs allowed to trigger applies (modes degraded and B)."
  type        = list(string)

  validation {
    condition     = alltrue([for id in var.approver_actor_ids : can(regex("^[0-9]+$", id))])
    error_message = "Use numeric GitHub user IDs (gh api users/LOGIN --jq .id), not logins."
  }
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false if the account already has one (it is then looked up)."
  type        = bool
  default     = true
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent state object versions expire."
  type        = number
  default     = 90
}
