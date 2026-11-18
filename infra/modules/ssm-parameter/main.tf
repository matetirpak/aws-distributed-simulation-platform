# A plain String parameter (never SecureString): cheap, instant to create and
# destroy, and easy to drift by editing a tag in the console.
resource "aws_ssm_parameter" "this" {
  name        = var.name
  description = var.description
  type        = "String"
  tier        = "Standard"
  value       = var.value
  tags        = var.tags
}
