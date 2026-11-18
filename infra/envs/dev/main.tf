module "pipeline_canary" {
  source = "../../modules/ssm-parameter"

  name        = "/${local.name_prefix}/${local.environment}/pipeline-canary"
  description = "Harmless canary managed by the Terraform CI/CD pipeline. Change it through a PR."
  value       = "hello from ${local.environment}"

  tags = {
    Purpose = "ci-cd-canary"
  }
}

module "dummy_vpc" {
  source            = "../../modules/vpc"
  name              = "${local.name_prefix}-${local.environment}-dummy"
  cidr_block        = "10.0.0.0/16"
  availability_zone = "${local.aws_region}a"
}

module "dummy_instance" {
  source    = "../../modules/ec2-instance"
  name      = "${local.name_prefix}-${local.environment}-dummy"
  vpc_id    = module.dummy_vpc.vpc_id
  subnet_id = module.dummy_vpc.public_subnet_id
}
