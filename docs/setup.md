# First-time setup (repo admin)

Order matters. Bootstrap **before** the first infrastructure PR, so its plan can assume the plan role.

1. GitHub settings (below)
2. AWS bootstrap (below)
3. The account variable `INFRA_DEV` (below)
4. Open or re-run a PR → green → review → merge
5. Approve the first `deploy` plan if it waits for one, by running `apply.yml`

## Prerequisites

- **Terraform 1.16.4** locally. The roots require `~> 1.16.4`, so older versions refuse. On Ubuntu with HashiCorp's apt repository:
  ```bash
  sudo apt update && sudo apt install terraform=1.16.4-1
  sudo apt-mark hold terraform   # stay on CI's version until .terraform-version is bumped
  ```
  `tfenv` or `mise` read `.terraform-version` instead.
- **Admin access** to the AWS account via SSO.

## 1. GitHub settings

CI never changes repository settings; run these yourself. Set `R=OWNER/NAME` for this repository.

**Actions permissions.** UI: Settings → Actions → General → allow selected actions → tick **Allow actions created by GitHub** → allow `hashicorp/setup-terraform@*, aws-actions/configure-aws-credentials@*` → tick **Require actions to be pinned to a full-length commit SHA**.
```bash
gh api -X PUT repos/$R/actions/permissions -F enabled=true -f allowed_actions=selected -F sha_pinning_required=true
gh api -X PUT repos/$R/actions/permissions/selected-actions --input - <<'EOF'
{"github_owned_allowed": true, "verified_allowed": false,
 "patterns_allowed": ["hashicorp/setup-terraform@*", "aws-actions/configure-aws-credentials@*"]}
EOF
```

**Workflow permissions.** UI: *Read repository contents*, and **untick** *Allow GitHub Actions to create and approve pull requests*.
```bash
gh api -X PUT repos/$R/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

**Fork PRs:** plans never run for forks (`ci-result` fails them). In a public repository, also require approval for workflows from outside contributors: Settings → Actions → General → *Fork pull request workflows*.

**Labels**
```bash
gh label create drift --repo $R --color D93F0B --description "Infrastructure differs from main (drift.yml)"
gh label create unreviewed-push --repo $R --color B60205 --description "Commit reached main without a merged PR (main-guard.yml)"
gh label create awaiting-approval --repo $R --color FBCA04 --description "A plan on main is waiting for an approver (deploy.yml)"
```

**Protect `main`** with a ruleset: require a pull request, require the status check `ci-result`, block force pushes and deletion. Rulesets need a public repository, or a paid plan for a private one. Without them, the IAM gate still decides who can apply, and `main-guard.yml` reports pushes that skipped a PR.

## 2. AWS bootstrap

This creates the state bucket and KMS key, the GitHub OIDC provider, and the `gha-terraform-plan` and `gha-terraform-apply` roles. You run it once per account, by hand; CI never applies it.

**Its inputs** go into a git-ignored `infra/bootstrap/dev/terraform.tfvars.json`. Start from [`terraform.tfvars.example.json`](../infra/bootstrap/dev/terraform.tfvars.example.json) next to it; the IDs come from:
```bash
gh api repos/$R --jq '{id, owner_id: .owner.id}'
gh api repos/$R/actions/oidc/customization/sub       # the subject prefix
gh api users/<approver> --jq .id
```

**Check for name clashes first** (read-only):
```bash
export AWS_PROFILE=<your-dev-profile> && aws sso login
aws sts get-caller-identity --query Account --output text      # must be the right account
aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text
aws iam get-role --role-name gha-terraform-plan  >/dev/null 2>&1 && echo "plan role EXISTS"
aws iam get-role --role-name gha-terraform-apply >/dev/null 2>&1 && echo "apply role EXISTS"
aws kms describe-key --key-id alias/<name_prefix>-tfstate --region eu-central-1 >/dev/null 2>&1 && echo "KMS alias EXISTS"
```
If a `…/token.actions.githubusercontent.com` provider already exists, add `create_oidc_provider = false` to the `module "ci_trust"` call in `infra/bootstrap/<env>/main.tf`; the module then reuses it. Anything else that exists must be resolved first.

**Plan, read, apply.** The bootstrap keeps its state in the bucket it created. Its name contains the account ID, so it's passed at init:
```bash
cd infra/bootstrap/dev
terraform init -backend-config="bucket=<name_prefix>-tfstate-<account>-<region>"
terraform plan -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
terraform output
```
**A new account** can't start there, because the bucket doesn't exist yet. Apply once with local state, using a git-ignored `backend_override.tf` that contains `terraform { backend "local" {} }`. Then move the override into `./garbage` and run `terraform init -migrate-state -backend-config="bucket=..."`.

The plan shows the trust policies only as `(known after apply)` the first time, because they reference the OIDC provider that doesn't exist yet. The offline tests pin what they will contain. **Verify them after the apply:**
```bash
aws iam get-role --role-name gha-terraform-plan  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition'
aws iam get-role --role-name gha-terraform-apply --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition'
```
Check that `terraform output -raw ci_json` matches `infra/envs/dev/ci.json`, and that `state_kms_alias` matches `infra/envs/dev/backend.tf`.

Re-apply it when the bootstrap itself changes: approvers, approval mode, role scope, or **the repository it trusts**, for example after moving or renaming it.

CI's apply role is denied writes to `bootstrap/*` in the state bucket.

## 3. The account variable

CI learns the account, its roles and the state bucket from the repository variable `INFRA_DEV` ([ci-cd.md](ci-cd.md#account-details)). Build it from the bootstrap's output plus the dev root's inputs:
```bash
cd infra/bootstrap/dev
terraform output -raw ci_variable \
  | jq -c --slurpfile tfvars ../../envs/dev/terraform.tfvars.json \
      '. + {tfvars: ($tfvars[0] | del(.account_id, .repository))}' \
  | gh variable set INFRA_DEV --repo $R
```
`infra/envs/dev/terraform.tfvars.json` is the same git-ignored file you use for local plans. Start it from [`terraform.tfvars.example.json`](../infra/envs/dev/terraform.tfvars.example.json). Update the variable whenever the inputs change.

Until the variable exists, CI runs only the checks that need no account, and `deploy.yml`, `drift.yml` and `main-guard.yml` skip their jobs ([ci-cd.md](ci-cd.md#at-a-glance)).

## Local plans

```bash
cd infra/envs/dev
terraform init -backend-config="bucket=<state bucket>"
terraform plan -lock=false     # reads terraform.tfvars.json
```
