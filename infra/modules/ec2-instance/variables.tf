variable "name" {
  description = "Name used for the instance's Name tag and for its security group, role and instance profile."
  type        = string
}

variable "vpc_id" {
  description = "VPC the security group is created in. Must be the VPC that owns var.subnet_id."
  type        = string
}

variable "subnet_id" {
  description = "Subnet the instance's network interface attaches to. Needs a route to the internet, or SSM never connects."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Not free tier: t3.micro bills by the hour while the instance runs."
  type        = string
  default     = "t3.micro"
}

variable "tags" {
  description = "Extra tags, merged with the provider's default_tags."
  type        = map(string)
  default     = {}
}
