terraform {
  # Keep in step with /.terraform-version (CI installs exactly that version).
  required_version = "~> 1.16.4"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.69.1"
    }
  }
}
