# State lives in the bucket created by infra/bootstrap/dev. Its name contains the account ID, so it
# is passed at init: `-backend-config="bucket=..."` (CI: the INFRA_DEV repository variable; local
# runs: docs/setup.md). Locking uses an S3
# lock object (use_lockfile, Terraform >= 1.11); there is no DynamoDB table.
# kms_key_id is required: with only `encrypt = true` the backend would request
# SSE-S3 and bypass the bucket's KMS key (the bucket policy rejects that).
terraform {
  backend "s3" {
    key          = "envs/dev/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    kms_key_id   = "alias/dsim-tfstate"
    use_lockfile = true
  }
}
