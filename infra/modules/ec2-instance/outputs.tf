output "instance_id" {
  description = "EC2 instance ID. Use it as the --target of aws ssm start-session."
  value       = aws_instance.this.id
}

