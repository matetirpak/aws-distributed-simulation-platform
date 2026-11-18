variable "cidr_block" {
  type = string
}

variable "availability_zone" {
  description = "AZ for public subnet (e.g. eu-central-1a)"
  type        = string
}

variable "name" {
  description = "Name tags for VPC and its networking resources"
  type        = string
}
