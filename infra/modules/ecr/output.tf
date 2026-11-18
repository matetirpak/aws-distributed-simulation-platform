output "repository_url" {
  description = "The URL of the ECR repository."
  value       = aws_ecr_repository.this.repository_url
}

output "arn" {
  description = "Parameter ARN."
  value       = aws_ecr_repository.this.arn
}
