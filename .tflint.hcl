# Repo-wide TFLint config. CI runs `tflint --recursive --config "$PWD/.tflint.hcl"`.
# The aws ruleset binary is installed by .github/scripts/install-tool.sh from a
# checksum-verified release (see .github/tools.lock), so `tflint --init` never
# downloads anything unverified. Bump `version` here and in tools.lock together.

config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.49.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
