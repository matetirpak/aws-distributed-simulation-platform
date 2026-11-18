# Per-environment values that aren't account details (those are inputs: variables.tf).
locals {
  environment = "dev"
  aws_region  = "eu-central-1"
  account_id  = var.account_id
  name_prefix = "dsim"

  alert_recipients = var.alert_recipients
}
