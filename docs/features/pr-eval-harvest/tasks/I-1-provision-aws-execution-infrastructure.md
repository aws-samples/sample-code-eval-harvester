---
id: I-1
title: "[Infra] Provision AWS execution infrastructure for the live eval run"
feature: pr-eval-harvest
workstream: Infrastructure
status: todo
complexity: M
implements: []
user_story: US-6
blocked_by: []
blocks: []
---

# I-1: [Infra] Provision AWS execution infrastructure for the live eval run

## Context

The eval harness (G-1–G-3) and the Lambda MicroVMs execution environment (H-1–H-4) wire into
`mise run eval`, which skips cleanly without credentials — that is by design. What makes it skip in
practice is that three things do not exist in the AWS account yet:

1. **An S3 bucket** (`MICROVM_BUCKET`) for the build artifact that `LambdaMicrovmsEnvironment`
   uploads before each trial (the task's Dockerfile + agentd binary, packed together).
2. **An IAM build role** (`MICROVM_BUILD_ROLE_ARN`) that the Lambda MicroVMs platform assumes during
   the image build. The caller passes its ARN; the platform uses it to access S3, write to CloudWatch,
   and publish the Firecracker snapshot.
3. **Caller IAM permissions** — whoever runs `mise run eval` needs `iam:PassRole` (to hand the build
   role to the platform), S3 read/write on the artifacts bucket, the `lambda:*Microvm*` actions (the
   service API the microvms bindings drive — MicroVMs authorizes under the `lambda` prefix), and
   `bedrock:InvokeModel` for the evaluation model.

There is also an **execution role** (`MICROVM_EXECUTION_ROLE_ARN`) that the running VM assumes. It is
required for grading, not optional: the emitted PR-review task seals the VM at `no-network`, so both
the agent-under-test and the verifier's judge reach Bedrock only through this role's credentials
(resolved the way an AWS SDK discovers an instance/container role — not env vars; host/caller
credentials passed in are ignored). Without the role's `bedrock:InvokeModel` grant the agent submits
nothing and the judge cannot adjudicate, so every trial scores zero.

This task writes Terraform under `infrastructure/terraform/` so any team member with an AWS account
can `terraform apply` and get a working environment.

**North star:** `export` the three lines `terraform output` prints, then run `mise run eval` without
a skip message.
**Implements:** none (execution-substrate infrastructure). · **User story:** US-6.

## Design References

- **`harvest_env/src/harvest_env/lambda_microvms.py`** — the env
  docstring lists the exact env vars; the `preflight` method lists what the boto3 check validates.
  The three mandatory env vars are `MICROVM_BUCKET`, `MICROVM_BUILD_ROLE_ARN`, and valid AWS
  credentials. `MICROVM_EXECUTION_ROLE_ARN` is optional.
- **H-2** — the environment's IAM surface: S3 put/delete for artifact upload, the build role passed
  to `build_image`, the `lambda-microvms` boto3 service (requires `boto3>=1.43.35`).
- **H-3** — import-path wiring; this task is the infrastructure side of the same item.
- **PRD §7 Constraints** — "Execution is Lambda MicroVMs", ARM64 only, `us-east-1` unless noted.

## What To Build

All under `infrastructure/terraform/`. Terraform ≥ 1.6, AWS provider `~> 5.0`.

1. **`provider.tf`** — AWS provider + `required_version`. No remote backend configured (local state
   for now; add a backend block when the team has a shared state bucket).
2. **`variables.tf`** — `aws_region` (default `us-east-1`), `project` (default `eval-harvest`),
   `eval_caller_principal_arns` (**optional**, default `[]` — the caller policy attaches to the
   Terraform caller only when it is an assumed role, detected via `aws_caller_identity`; set this to
   a dedicated runner **role** ARN for live runs — role ARNs only, never an IAM user), `eval_model_ids` (Bedrock model IDs the caller may invoke; defaults
   to the Sonnet 4.5 cross-region inference profile), `agent_model_ids` (Bedrock model IDs the
   agent-under-test may invoke from inside the VM via the execution role) and `judge_model_id` (the
   single cross-region inference profile the verifier's judge invokes; must match the model pinned in
   the verifier's `judge.toml`). The `agent_model_ids` / `judge_model_id` grants live on the execution
   role, not the caller.
3. **`s3.tf`** — `aws_s3_bucket` for build artifacts (private, SSE-AES256, public-access-blocked,
   lifecycle rule to expire artifacts after 7 days), named `${var.project}-microvm-artifacts`.
4. **`iam.tf`** — three IAM objects. Trust `lambda.amazonaws.com`, and grant the MicroVMs API
   under the `lambda:` action prefix — **not** `lambda-microvms.amazonaws.com` or
   `lambda-microvms:*`, neither of which exists (see Notes & Gotchas; get this wrong and
   `terraform apply` dies on `CreateRole`):
   - **Build role** (`${project}-microvm-build`) — trusted by `lambda.amazonaws.com`;
     policy: S3 get/put/delete/list on the artifacts bucket, `logs:CreateLogGroup` /
     `logs:CreateLogDelivery` / `logs:PutLogEvents` for `/aws/lambda-microvms/*` in CloudWatch
     (the log group the platform writes to is `/aws/lambda-microvms/<image-name>`).
   - **Caller policy** (`${project}-eval-caller`) — its policy document is written inline with
     `jsonencode()` (not `templatefile()`): a template reference is opaque to the IAM static
     analyzers, so an external policy file drops these statements out of scanner scope; inline HCL
     keeps every Action/Resource in view. Grants: `iam:PassRole` (scoped to the build and execution role ARNs),
     `s3:PutObject` / `s3:GetObject` / `s3:DeleteObject` / `s3:ListBucket` on the artifacts bucket,
     the MicroVMs API actions enumerated from the botocore model — every `lambda:*Microvm*` operation
     plus `lambda:TagResource` / `lambda:UntagResource` / `lambda:ListTags`, since `build_image`
     passes `harbor:*` tags on `CreateMicrovmImage` — and `bedrock:InvokeModel` /
     `bedrock:InvokeModelWithResponseStream` scoped to `eval_model_ids`. Enumerate rather than
     granting `lambda:*`, which is all of Lambda. **Attach it non-exclusively, to roles only**: one
     `aws_iam_role_policy_attachment` per role (`for_each` over the derived role-name list), matching
     the build/execution role attachments. Never attach the policy directly to an IAM user — that is
     the checkov CKV_AWS_40 anti-pattern and contradicts the S1 runner-role guidance (a dedicated
     role with temporary credentials, no long-lived user keys). So `eval_caller_principal_arns`
     accepts role ARNs only (enforce with a variable `validation` that rejects any non-`:role/` ARN),
     and the empty-list fallback targets the Terraform caller only when it is an assumed role. Do
     **not** use `aws_iam_policy_attachment` — it manages the policy's attachments exclusively and
     would detach the policy from every principal not listed here on each apply (and from all of them
     on destroy).
   - **Execution role** (`${project}-microvm-execution`) — trusted by `lambda.amazonaws.com`; the
     role the running VM assumes. Its policy grants CloudWatch logs **and** `bedrock:InvokeModel` /
     `bedrock:InvokeModelWithResponseStream` for the two Bedrock paths reached from inside the sealed
     VM: the verifier's judge (`judge_model_id`) and the agent-under-test (`agent_model_ids`). Because
     the VM is `no-network` with credentials resolved from the instance role, this grant is what lets
     the judge adjudicate and the agent submit findings — without it every trial AccessDenies and
     scores zero. Scope each Bedrock grant to the inference-profile ARN **and** the underlying
     foundation-model ARN (the profile ID with its `us.` prefix stripped), with a region wildcard for
     cross-region routing. Never `Resource = "*"`.
5. **`outputs.tf`** — `microvm_bucket`, `microvm_build_role_arn`, `microvm_execution_role_arn`,
   and a `mise_run_eval_exports` output that prints the three `export` lines ready to paste.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `infrastructure/terraform/provider.tf` | Create | AWS provider + version constraints |
| `infrastructure/terraform/variables.tf` | Create | `aws_region`, `project`, `eval_caller_principal_arns`, `eval_model_ids`, `agent_model_ids`, `judge_model_id` |
| `infrastructure/terraform/s3.tf` | Create | Artifacts bucket |
| `infrastructure/terraform/iam.tf` | Create | Build role, execution role (Bedrock for judge + agent, ARN-scoped), caller policy (non-exclusive per-role attachments; roles only) |
| `infrastructure/terraform/outputs.tf` | Create | Bucket name, role ARNs, paste-ready export block |

## Verification Commands

```bash
mise run infra-check         # checkov: 0 failed
mise run infra-plan          # terraform init + plan (no -var needed; caller auto-detected)

# Prove the trust principal before applying: IAM validates the Service field against its
# registry, so a wrong name only surfaces as a CreateRole failure. Create a throwaway role,
# then delete it. Expected: lambda.amazonaws.com succeeds, every *microvms* spelling fails
# with MalformedPolicyDocument / Invalid principal in policy.
aws iam create-role --role-name principal-probe --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam delete-role --role-name principal-probe

# Prove the action prefix. Expected: no findings. Swapping lambda: for lambda-microvms:
# must produce INVALID_SERVICE_IN_ACTION — watch it go red, or the check is decorative.
aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["lambda:RunMicrovm","lambda:CreateMicrovmImage"],"Resource":"*"}]}'

mise run infra-apply         # terraform init + apply

# After apply, mise run eval reads the outputs itself (scripts/run-eval.sh):
mise run eval -- -m us.anthropic.claude-sonnet-4-5-20251001
# Expected: no skip message; eval runs 2 objectives × 3 trials
```

## Acceptance Criteria

- [ ] `terraform validate` is clean and `mise run infra-check` (checkov) reports 0 failures
- [ ] `mise run infra-plan` produces no errors with no `-var` flags (caller identity auto-detected)
- [ ] The trust principal and the action prefix are proved against IAM, not assumed: the
      `create-role` probe accepts `lambda.amazonaws.com`, and `validate-policy` returns no
      findings for the caller policy's action list
- [ ] `mise run infra-apply` creates: one S3 bucket, the build role, the execution role, and a caller
      policy attached to the Terraform caller (or the override principals when set)
- [ ] The execution role's policy grants `bedrock:InvokeModel` for both `judge_model_id` and
      `agent_model_ids`, each scoped to the inference-profile ARN and the foundation-model ARN (no
      `Resource = "*"`), and the caller policy is attached non-exclusively to roles only via
      per-role `aws_iam_role_policy_attachment` (no `aws_iam_user_policy_attachment`, no `aws_iam_policy_attachment`), with `eval_caller_principal_arns` validated to role ARNs
- [ ] `terraform output -raw mise_run_eval_exports` prints three valid `export` lines
- [ ] Setting those exports and running `mise run eval -m <model>` (with Bedrock enabled) produces
      no skip message and starts a trial

## Out Of Scope

- Remote Terraform state backend (add a `backend` block when you have a shared state bucket).
- Bedrock model access enablement — that is a console/API step per AWS account, not Terraform.
  The caller policy grants `bedrock:InvokeModel`; the model itself must be enabled in the account
  at `Bedrock → Model access → Manage model access`.
- ECR repository or container build pipeline — Lambda MicroVMs builds the container server-side
  using the uploaded artifact; no ECR is needed.
- CI/CD pipeline for the eval — separate concern.

## Notes & Gotchas

- **Use `lambda.amazonaws.com` as the trust principal and `lambda:` as the action prefix.**
  The tempting spelling — `lambda-microvms` in both positions — is wrong in both, and this is the
  single easiest way to lose an afternoon on this task. Lambda MicroVMs has its own botocore model
  (`lambda-microvms/2025-09-09`) but not its own IAM namespace: that model's `endpointPrefix` and
  `signingName` are both `lambda`. `CreateRole` with `lambda-microvms.amazonaws.com` (also
  `microvms.amazonaws.com`, also `microvms.lambda.amazonaws.com`) fails with
  `MalformedPolicyDocument: Invalid principal in policy`, and `lambda-microvms:*` in an action
  fails Access Analyzer with `INVALID_SERVICE_IN_ACTION`. Don't take this note on faith — the
  Verification Commands below re-prove both halves against IAM in about ten seconds, and the
  service is new enough that the answer could move.
- **Derive the action list from the model, not from memory.** `python -c` over
  `botocore/data/lambda-microvms/<version>/service-2.json.gz` lists the operations; prefix each
  with `lambda:`. If a later botocore adds operations, the caller policy needs them too — a
  missing one shows up as `AccessDenied` mid-trial, not at apply time.
- **Bedrock model access is two-layered.** The caller policy grants the IAM permission; the model
  still needs to be enabled per-account in the Bedrock console (`Model access`). `terraform apply`
  does not enable models — do that manually before the first eval run.
- **ARM64 only.** The `LambdaMicrovmsEnvironment` validates that the task image is built for
  `linux/arm64`; no Terraform resource controls this, but the `agentd` binary it downloads must
  match the architecture.
- **The execution role is required for grading, not optional.** The emitted PR-review task seals the
  VM at `no-network`, so the agent-under-test and the verifier's judge reach Bedrock only through this
  role — host/caller credentials passed in are ignored and the SDK falls back to the instance role.
  Its `bedrock:InvokeModel` grant must cover both `agent_model_ids` and `judge_model_id`, each scoped
  to the inference-profile ARN plus the underlying foundation-model ARN (the profile ID minus its
  `us.` prefix), region wildcard, never `Resource = "*"`. Miss it and the agent submits nothing / the
  judge AccessDenies, and the trial scores zero.
- **Leave the confused-deputy condition off the trust policies for now.** An `aws:SourceAccount`
  condition on the two `sts:AssumeRole` statements is the usual hardening, but whether MicroVMs
  passes that context key is unverified, and a condition the service does not satisfy turns into a
  mystery `AssumeRole` denial mid-build. Add it after the first successful build, when you can
  break it deliberately and watch the failure.
- **Runner role, not a user** — the caller policy attaches to IAM roles only, never directly to an
  IAM user (CKV_AWS_40; S1 wants a dedicated runner role with temporary credentials, no long-lived
  user keys). With an empty `eval_caller_principal_arns`, it falls back to the Terraform caller only
  when that caller is an assumed role. For live runs, set `eval_caller_principal_arns` to a dedicated
  runner role ARN.

## Dependencies

**Blocked by:** nothing — can be done independently of the rest of the feature board.
**Blocks:** nothing (this unblocks the live eval run, which is a PRD `[TODO]`, not a board task).
