# The dev account's monthly cost budget: alerts at 50, 80 and 100% of actual spend, plus one
# when the forecast passes 100%, by email to the alert list. Between batches, dev costs about
# $66 a month (the database and the VPC endpoints); each batch of 1,000 matches about $12.
#
# It counts usage before credits. With credits counted, the account's credits cancel the
# usage, the budget sees $0, and it would stay silent until the credits run out.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name_prefix}-${local.environment}-monthly"
  budget_type  = "COST"
  limit_amount = "120"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
  }

  dynamic "notification" {
    for_each = toset([50, 80, 100])
    content {
      notification_type          = "ACTUAL"
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = local.alert_recipients
    }
  }

  notification {
    notification_type          = "FORECASTED"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = local.alert_recipients
  }
}

# What the credits don't cover: an alert as soon as $1 a month is billed after credits. Credits
# don't apply to everything (Marketplace, support plans, some purchases), and they run out.
resource "aws_budgets_budget" "after_credits" {
  name         = "${local.name_prefix}-${local.environment}-after-credits"
  budget_type  = "COST"
  limit_amount = "10"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    notification_type          = "ACTUAL"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 10
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = local.alert_recipients
  }
}
