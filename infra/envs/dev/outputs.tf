output "pipeline_canary_name" {
  description = "Name of the canary SSM parameter."
  value       = module.pipeline_canary.name
}

output "game_controller_repository_url" {
  description = "ECR repository the game repository's release workflow pushes to (repository variable ECR_REPOSITORY_URL there)."
  value       = module.game_controller_ecr.repository_url
}

output "game_controller_push_role_arn" {
  description = "Role the game repository's release workflow assumes to push (repository variable AWS_ECR_PUSH_ROLE_ARN there)."
  value       = aws_iam_role.game_server_ecr_push.arn
}

output "dummy_instance_id" {
  description = "EC2 instance ID of dummy box."
  value       = module.dummy_instance.instance_id
}

output "match_repository_url" {
  description = "ECR repository of the match image (platform/prototype/push-image.sh)."
  value       = module.match_ecr.repository_url
}

output "match_replay_bucket" {
  description = "Where match tasks store their replays."
  value       = aws_s3_bucket.match_replays.bucket
}

output "match_dashboard_url" {
  description = "The CloudWatch dashboard for batches of matches."
  value       = "https://${local.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${local.aws_region}#dashboards/dashboard/${aws_cloudwatch_dashboard.matches.dashboard_name}"
}
