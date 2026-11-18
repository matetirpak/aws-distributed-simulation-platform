# The bootstrap's own state, in the bucket it created (key bootstrap/dev/). The bucket's name
# contains the account ID, so it's passed at init: `-backend-config="bucket=..."`. A new account's
# first apply can't use a bucket that doesn't exist yet, so it runs with local state; then
# this file is added and `terraform init -migrate-state` moves it here.
# CI's apply role is denied writes to bootstrap/* (ProtectBootstrapState).
# kms_key_id is required: with only `encrypt = true` the backend would request SSE-S3 and
# bypass the bucket's KMS key (the bucket policy rejects that).
terraform {
  backend "s3" {
    key          = "bootstrap/dev/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    kms_key_id   = "alias/dsim-tfstate"
    use_lockfile = true
  }
}
