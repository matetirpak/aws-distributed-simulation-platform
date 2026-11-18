# Alerts on spend, capacity and matches, by email to the alert list (var.alert_recipients; the
# budgets are in budget.tf). Each alarm also mails when it
# clears. They show together on the matches dashboard (matches_monitoring.tf).
locals {
  alert_prefix = "${local.name_prefix}-${local.environment}"
  alert_topic  = [aws_sns_topic.alerts.arn]

  # What runs now, as the account's quotas count it (AWS/Usage, published every minute, free).
  usage = {
    ec2_spot      = { Service = "EC2", Type = "Resource", Resource = "vCPU", Class = "Standard/Spot" }
    ec2_on_demand = { Service = "EC2", Type = "Resource", Resource = "vCPU", Class = "Standard/OnDemand" }
    fargate       = { Service = "Fargate", Type = "Resource", Resource = "vCPU", Class = "Standard/OnDemand" }
  }

  # For the dashboard's alarm panel.
  alert_alarms = concat(
    [
      aws_cloudwatch_metric_alarm.machines_left_running.arn,
      aws_cloudwatch_metric_alarm.tasks_left_running.arn,
      aws_cloudwatch_metric_alarm.spot_quota.arn,
      aws_cloudwatch_metric_alarm.matches_voided.arn,
    ],
    [for alarm in aws_cloudwatch_metric_alarm.match_database : alarm.arn],
  )
}

# The alert list's topic: alarms publish here, and each recipient gets an email. Encrypted with a
# key of its own, because CloudWatch can't publish to a topic under the AWS-managed SNS key.
resource "aws_kms_key" "alerts" {
  description             = "Encrypts the SNS topic ${local.alert_prefix}-alerts. Managed by Terraform."
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Delegates to IAM (account root), like the state key: who may use this key is decided
        # by the role policies. "*" in a key policy means this key only.
        Sid       = "EnableIamPolicies"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AlarmsPublishToTheTopic"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
    ]
  })
}

resource "aws_kms_alias" "alerts" {
  name          = "alias/${local.alert_prefix}-alerts"
  target_key_id = aws_kms_key.alerts.key_id
}

resource "aws_sns_topic" "alerts" {
  name              = "${local.alert_prefix}-alerts"
  kms_master_key_id = aws_kms_key.alerts.arn
}

# Any of this environment's alarms may publish.
resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AlarmsPublish"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alerts.arn
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:${local.aws_region}:${local.account_id}:alarm:${local.alert_prefix}-*" }
      }
    }]
  })
}

# One subscription per address, so an unsubscribe link removes only that person. drift.yml
# reports a removed subscription, and the next apply restores it.
resource "aws_sns_topic_subscription" "alerts" {
  for_each  = toset(local.alert_recipients)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# Cost anomalies: AWS's own detection, per service. An account has at most one such monitor. If
# AWS already created it (var.cost_anomaly_monitor_id), it's imported; otherwise Terraform creates
# it. Any default subscription stays as it is; this one mails the alert list from $10.
import {
  for_each = var.cost_anomaly_monitor_id == null ? toset([]) : toset([var.cost_anomaly_monitor_id])
  to       = aws_ce_anomaly_monitor.services
  id       = "arn:aws:ce::${local.account_id}:anomalymonitor/${each.value}"
}

resource "aws_ce_anomaly_monitor" "services" {
  name              = "Default-Services-Monitor"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "alert_list" {
  name             = "${local.alert_prefix}-cost-anomalies"
  frequency        = "DAILY"
  monitor_arn_list = [aws_ce_anomaly_monitor.services.arn]

  dynamic "subscriber" {
    for_each = local.alert_recipients
    content {
      type    = "EMAIL"
      address = subscriber.value
    }
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = ["10"]
    }
  }
}

# Nothing should run for long. A bot machine lives for one match, a few minutes, and a batch of
# 1,000 matches takes about 15. So EC2 instances running all of the last 90 minutes, or Fargate
# tasks all of the last 2 hours (a match stops itself after 1), mean something didn't end.
resource "aws_cloudwatch_metric_alarm" "machines_left_running" {
  alarm_name          = "${local.alert_prefix}-machines-left-running"
  alarm_description   = "EC2 instances have been running for 90 minutes. Bot machines live for one match and no batch runs this long: look for machines that didn't end (matches_bot_hosts.tf)."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 18
  datapoints_to_alarm = 18
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alert_topic
  ok_actions          = local.alert_topic

  metric_query {
    id          = "running"
    expression  = "FILL(spot, 0) + FILL(on_demand, 0)"
    label       = "EC2 vCPUs running"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = { spot = local.usage.ec2_spot, on_demand = local.usage.ec2_on_demand }
    content {
      id = metric_query.key
      metric {
        namespace   = "AWS/Usage"
        metric_name = "ResourceCount"
        dimensions  = metric_query.value
        period      = 300
        stat        = "Minimum"
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "tasks_left_running" {
  alarm_name          = "${local.alert_prefix}-tasks-left-running"
  alarm_description   = "Fargate tasks have been running for 2 hours. A match task stops itself after 1 hour at most: look for match tasks that didn't end (cluster ${local.matches_name})."
  namespace           = "AWS/Usage"
  metric_name         = "ResourceCount"
  dimensions          = local.usage.fargate
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 24
  datapoints_to_alarm = 24
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alert_topic
  ok_actions          = local.alert_topic
}

# Spot machines past 90% of the account's Spot quota. The largest planned batch uses about 80%.
resource "aws_cloudwatch_metric_alarm" "spot_quota" {
  alarm_name          = "${local.alert_prefix}-spot-quota"
  alarm_description   = "Spot vCPUs in use passed 90% of the quota (All Standard Spot Instance Requests). Bots that don't fit wait for machines or fall back to on-demand."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 90
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alert_topic
  ok_actions          = local.alert_topic

  metric_query {
    id          = "share"
    expression  = "100 * spot / SERVICE_QUOTA(spot)"
    label       = "Spot vCPUs, % of the quota"
    return_data = true
  }

  metric_query {
    id = "spot"
    metric {
      namespace   = "AWS/Usage"
      metric_name = "ResourceCount"
      dimensions  = local.usage.ec2_spot
      period      = 300
      stat        = "Maximum"
    }
  }
}

# The results database: db.t4g.micro serves about 80 connections, and has 20 GiB of storage.
resource "aws_cloudwatch_metric_alarm" "match_database" {
  for_each = {
    connections = {
      metric = "DatabaseConnections", stat = "Maximum", operator = "GreaterThanThreshold", threshold = 70,
      text   = "Connections passed 70 of about 80. Match tasks retry with jitter, but a batch this busy may lose results."
    }
    storage = {
      metric = "FreeStorageSpace", stat = "Minimum", operator = "LessThanThreshold", threshold = 2 * 1024 * 1024 * 1024,
      text   = "Less than 2 GiB of storage left."
    }
  }
  alarm_name          = "${local.alert_prefix}-match-database-${each.key}"
  alarm_description   = "Results database ${aws_db_instance.matches.identifier}: ${each.value.text}"
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  dimensions          = { DBInstanceIdentifier = aws_db_instance.matches.identifier }
  statistic           = each.value.stat
  period              = 60
  evaluation_periods  = 2
  comparison_operator = each.value.operator
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alert_topic
  ok_actions          = local.alert_topic
}

# Voided matches: a void never counts for or against a bot, but many of them mean the
# infrastructure is failing. Counted from each match task's "match saved" line.
resource "aws_cloudwatch_log_metric_filter" "match_results" {
  for_each = {
    MatchesSaved  = "{ $.message = \"match saved\" }"
    MatchesVoided = "{ $.message = \"match saved\" && $.status = \"void*\" }"
  }
  name           = "${local.matches_name}-${each.key}"
  log_group_name = aws_cloudwatch_log_group.matches.name
  pattern        = each.value

  metric_transformation {
    name      = each.key
    namespace = "${local.name_prefix}/matches"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "matches_voided" {
  alarm_name          = "${local.alert_prefix}-matches-voided"
  alarm_description   = "More than 5% of the matches saved in 15 minutes were voided (at least 20 matches). The dashboard's results table shows which statuses."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alert_topic
  ok_actions          = local.alert_topic

  metric_query {
    id          = "rate"
    expression  = "IF(saved >= 20, 100 * FILL(voided, 0) / saved, 0)"
    label       = "Voided, % of matches saved"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = { saved = "MatchesSaved", voided = "MatchesVoided" }
    content {
      id = metric_query.key
      metric {
        namespace   = "${local.name_prefix}/matches"
        metric_name = metric_query.value
        period      = 900
        stat        = "Sum"
      }
    }
  }

  depends_on = [aws_cloudwatch_log_metric_filter.match_results]
}
