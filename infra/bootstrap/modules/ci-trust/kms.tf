# Customer-managed key for state. The key policy delegates to IAM (account root);
# who may decrypt is decided by the role policies. Decrypts are logged in CloudTrail.
resource "aws_kms_key" "state" {
  description             = "Encrypts Terraform state in ${local.bucket_name}"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EnableIamPolicies"
      Effect    = "Allow"
      Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
      Action    = "kms:*"
      Resource  = "*"
    }]
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "state" {
  name          = local.kms_alias
  target_key_id = aws_kms_key.state.key_id
}
