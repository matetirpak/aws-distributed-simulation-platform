variable "name" {
  description = "Fully qualified parameter name, e.g. /team/dev/thing."
  type        = string

  validation {
    condition     = startswith(var.name, "/")
    error_message = "The parameter name must start with \"/\"."
  }
}

variable "description" {
  description = "Human-readable description shown in the SSM console."
  type        = string
}

variable "value" {
  description = "Plain-text value. Never put secrets here: the value is visible in plans, PR comments and state."
  type        = string
}

variable "tags" {
  description = "Extra tags, merged with the provider's default_tags."
  type        = map(string)
  default     = {}
}
