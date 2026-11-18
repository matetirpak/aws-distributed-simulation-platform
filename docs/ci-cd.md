# Terraform CI/CD

`main` is the single source of truth for the AWS infrastructure in [`infra/`](../infra). A change reaches AWS only as a **saved plan from `main`**: in dev, applied right after a reviewed merge when it destroys nothing; otherwise read and applied by an approver.

```
PR ─► checks + plan comment ─► review & merge ─► plan on main ─┬─► dev, nothing destroyed: applied automatically
                                                                └─► otherwise: approver runs apply.yml ─► applied
```

Setting up a copy (bootstrap, GitHub settings, the account variable): [setup.md](setup.md).

## At a glance

- **Approval mode:** *degraded*: the approver applies by running `apply.yml`, and IAM enforces who and which workflow.
- **`main` is protected** by a ruleset: a PR and a green `ci-result` ([setup](setup.md#1-github-settings)).
- **Approvers:** the GitHub user IDs in `infra/envs/<env>/ci.json` (`approver_ids`), which must match the bootstrap's `approver_actor_ids`.
- **AWS:** region `eu-central-1`. The account, the CI roles and the state bucket are never committed: they're in the repository variable `INFRA_<ENV>` ([below](#account-details)).

| Workflow | When | Does |
|---|---|---|
| `ci.yml` | every PR to `main`, every push to `main` | checks, plan comment (PRs only), `ci-result` |
| `deploy.yml` | push to `main` | plans each environment; applies dev's plan if it destroys nothing; otherwise opens a "Plan awaiting approval" issue |
| `apply.yml` | you run it | applies an approved saved plan |
| `drift.yml` | weekdays 05:00 CET | opens or closes `Drift detected` issues |
| `main-guard.yml` | every push to `main` | alarm if `main` moved without a merged PR |

**Without an AWS account.** Everything that needs AWS waits for an `INFRA_<ENV>` variable: until one exists, `ci.yml` skips the plan, and `deploy.yml`, `drift.yml` and `main-guard.yml` skip their jobs. The code checks (Rust, Python, contract, workflow lint) and the Terraform checks that need no account (`fmt`, `validate`, `tflint`, the IaC scan) run either way. On a push to `main`, `ci.yml` checks the whole repository.

## Account details

Nothing in the repository names an AWS account. Each environment has one **repository variable**, `INFRA_<ENV>` (for dev: `INFRA_DEV`), holding one JSON object:

```json
{
  "plan_role_arn": "arn:aws:iam::<account>:role/gha-terraform-plan",
  "apply_role_arn": "arn:aws:iam::<account>:role/gha-terraform-apply",
  "state_bucket": "<name_prefix>-tfstate-<account>-<region>",
  "tfvars": { "alert_recipients": ["..."], "game_server": { "...": "..." }, "cost_anomaly_monitor_id": "..." }
}
```

- The first three come from the bootstrap: `terraform output -raw ci_variable` in `infra/bootstrap/<env>`.
- `tfvars` holds the root's inputs (`infra/envs/<env>/variables.tf`), except `account_id` and `repository`, which CI sets itself: from the role ARN and from `GITHUB_REPOSITORY`.
- [`read-ci-config.sh`](../.github/scripts/read-ci-config.sh) reads it and checks it. Then `tf-plan.sh` passes the bucket to `terraform init` and writes the inputs to a git-ignored `ci.auto.tfvars.json`.
- These are *variables*, not secrets: plans and logs may show them, and so may PR comments. Never put a secret in them.

How to set it: [setup.md](setup.md#3-the-account-variable).
