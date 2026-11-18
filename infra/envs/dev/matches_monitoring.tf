# One page for how a batch of matches is going (docs/architecture.md).
# The match logs only hold what a task managed to write. A task that dies first (out of memory,
# failed image pull, no capacity) is only in ECS's stop event, which ECS forgets after about an
# hour, so that event is kept in its own log group.
locals {
  match_logs      = aws_cloudwatch_log_group.matches.name
  match_stop_logs = aws_cloudwatch_log_group.match_stops.name

  match_widgets = [
    {
      title = "Matches playing, per minute"
      view  = "timeSeries"
      query = "SOURCE '${local.match_logs}' | filter message = \"match progress\" | stats count_distinct(@logStream) as playing by bin(1m)"
    },
    {
      title = "Results per batch"
      view  = "table"
      query = "SOURCE '${local.match_logs}' | filter message = \"match saved\" | stats count(*) as matches by batch_id, status, controller_exit"
    },
    {
      title = "Tasks that stopped abnormally, with the reason"
      view  = "table"
      query = "SOURCE '${local.match_stop_logs}' | filter detail.stopCode != \"EssentialContainerExited\" or detail.containers.0.exitCode != 0 | fields @timestamp, detail.startedBy as batch, detail.group as match, detail.stopCode as code, detail.containers.0.exitCode as exit, detail.stoppedReason as reason | sort @timestamp desc | limit 50"
    },
    {
      title = "Warnings and errors in the match logs"
      view  = "table"
      query = "SOURCE '${local.match_logs}' | filter level in [\"WARN\", \"ERROR\"] or message like /failed/ | fields @timestamp, @logStream, level, message, error | sort @timestamp desc | limit 50"
    },
    {
      title = "Latest results"
      view  = "table"
      query = "SOURCE '${local.match_logs}' | filter message = \"match saved\" | fields @timestamp, match_id, status, strategy_a, strategy_b, winner, win_points_a, win_points_b, timeouts, invalid | sort @timestamp desc | limit 50"
    },
    {
      title = "Slowest matches in progress"
      view  = "table"
      query = "SOURCE '${local.match_logs}' | filter message = \"match progress\" | stats latest(turn) as current_turn, latest(elapsed_s) as seconds, latest(@timestamp) as last_seen by @logStream | sort last_seen desc, current_turn asc | limit 20"
    },
  ]
}

resource "aws_cloudwatch_log_group" "match_stops" {
  name              = "/aws/events/${local.matches_name}-stops"
  retention_in_days = 14
}

resource "aws_cloudwatch_event_rule" "match_stops" {
  name        = "${local.matches_name}-stops"
  description = "Every stopped match task, with its stop code, exit code and reason"
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.matches.arn]
      lastStatus = ["STOPPED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "match_stops" {
  rule = aws_cloudwatch_event_rule.match_stops.name
  arn  = aws_cloudwatch_log_group.match_stops.arn
}

# EventBridge writes to CloudWatch Logs through a resource policy, not a role.
resource "aws_cloudwatch_log_resource_policy" "match_stops" {
  policy_name = "${local.matches_name}-stops"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.match_stops.arn}:*"
    }]
  })
}

resource "aws_cloudwatch_dashboard" "matches" {
  dashboard_name = local.matches_name
  dashboard_body = jsonencode({
    start = "-P90D" # past batches stay in view
    widgets = concat(
      [for i, widget in local.match_widgets : {
        type   = "log"
        x      = i % 2 * 12
        y      = floor(i / 2) * 7
        width  = 12
        height = 7
        properties = {
          title  = widget.title
          view   = widget.view
          query  = widget.query
          region = local.aws_region
        }
      }],
      [{
        type   = "metric"
        x      = 0
        y      = ceil(length(local.match_widgets) / 2) * 7
        width  = 24
        height = 6
        properties = {
          title  = "Results database"
          view   = "timeSeries"
          region = local.aws_region
          period = 60
          stat   = "Maximum"
          metrics = [
            ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", aws_db_instance.matches.identifier],
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.matches.identifier, { yAxis = "right" }],
          ]
        }
      }],
      # The alerts of alerts.tf, red when firing.
      [{
        type   = "alarm"
        x      = 0
        y      = ceil(length(local.match_widgets) / 2) * 7 + 6
        width  = 24
        height = 3
        properties = {
          title  = "Alerts"
          alarms = local.alert_alarms
        }
      }]
    )
  })
}
