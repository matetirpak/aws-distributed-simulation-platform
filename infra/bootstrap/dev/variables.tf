# What ties the bootstrap to one AWS account and one repository. It's applied by hand, so these
# come from a git-ignored terraform.tfvars.json next to this file (docs/setup.md). Sources:
#   gh api repos/OWNER/NAME --jq '{id, owner_id: .owner.id}'
#   gh api repos/OWNER/NAME/actions/oidc/customization/sub
#   gh api users/LOGIN --jq .id

variable "account_id" {
  description = "The AWS account to bootstrap. Terraform refuses to run against any other."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "github_repository" {
  description = "OWNER/NAME of the repository whose workflows may plan and apply, exactly as GitHub reports it."
  type        = string
}

variable "github_repository_id" {
  description = "Its immutable numeric ID."
  type        = string
}

variable "github_repository_owner_id" {
  description = "The immutable numeric ID of its owner."
  type        = string
}

variable "github_oidc_subject_prefix" {
  description = "The repository's OIDC `sub` prefix (sub_claim_prefix)."
  type        = string
}

variable "approver_actor_ids" {
  description = "Immutable GitHub user IDs allowed to trigger applies."
  type        = list(string)
}
