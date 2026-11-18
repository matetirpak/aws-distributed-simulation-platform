# What ties this root to one AWS account and to people: none of it is committed. CI passes it from
# the repository variable INFRA_DEV (docs/ci-cd.md); local runs read a git-ignored
# terraform.tfvars.json (docs/setup.md).

variable "account_id" {
  description = "The AWS account of this environment. Terraform refuses to run against any other. CI sets it from the role ARN."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "repository" {
  description = "The repository that manages these resources (OWNER/NAME), for the Repository tag. CI sets it."
  type        = string
}

variable "alert_recipients" {
  description = "Who receives the alarms and budget alerts by email (alerts.tf, budget.tf). Each address confirms AWS's subscription email once."
  type        = list(string)
}

variable "game_server" {
  description = "The game's repository that may push controller images (game_controller.tf): OWNER/NAME, its immutable repository and owner IDs (`gh api repos/OWNER/NAME --jq '{id, owner_id: .owner.id}'`), and its OIDC subject prefix (`gh api repos/OWNER/NAME/actions/oidc/customization/sub`)."
  type = object({
    repository     = string
    id             = string
    owner_id       = string
    subject_prefix = string
  })
}

variable "cost_anomaly_monitor_id" {
  description = "The ID of the account's AWS services cost anomaly monitor, if AWS already created one: the UUID at the end of its ARN (aws ce get-anomaly-monitors). Terraform imports it (alerts.tf). Leave it null in an account without one, and Terraform creates it."
  type        = string
  default     = null
  validation {
    condition     = var.cost_anomaly_monitor_id == null || can(regex("^[0-9a-f-]{36}$", var.cost_anomaly_monitor_id))
    error_message = "cost_anomaly_monitor_id must be the UUID at the end of the monitor's ARN."
  }
}
