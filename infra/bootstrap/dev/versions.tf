# Bootstrap root for the dev AWS account. Applied once by a human with admin SSO
# credentials (never by CI). Its state is in the bucket it created (backend.tf); a new account
# starts with local state (docs/setup.md).
terraform {
  required_version = "~> 1.16.4"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.69.1"
    }
  }
}
