variable "aws_region" {
  description = "AWS region for all resources. Lambda MicroVMs requires us-east-1 unless the platform has expanded."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name used as a prefix for resource names and tags."
  type        = string
  default     = "eval-harvest"
}

variable "eval_caller_principal_arns" {
  description = <<-EOT
    Principals receiving the caller policy (which grants S3 put/get/delete
    on the artifacts bucket, iam:PassRole for the build and execution roles, the lambda:*Microvm*
    actions, and bedrock:InvokeModel for the configured model IDs).

    The documented sample setup requires an explicit dedicated runner role in a
    disposable AWS account, separate from the provisioning identity (S1; see README.md).
    The legacy empty-list default attaches to the Terraform caller (only when it
    is an assumed role); it does not constrain that identity's other permissions.
    Do not use that default for live sample runs. This variable does not enforce
    account isolation or least privilege. Role ARNs only — the policy is never
    attached directly to an IAM user (S1; no long-lived user keys).
    Example: ["arn:aws:iam::123456789012:role/eval-harvest-runner"]
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.eval_caller_principal_arns : can(regex(":role/", arn))])
    error_message = "eval_caller_principal_arns accepts IAM role ARNs only (arn:aws:iam::<acct>:role/<name>); the caller policy is never attached directly to an IAM user."
  }
}

variable "eval_model_ids" {
  description = <<-EOT
    Bedrock model IDs the eval is allowed to invoke.
    NOTE: IAM permission is necessary but not sufficient — models must also be enabled
    per-account at Bedrock → Model access → Manage model access in the AWS console.
    The cross-region inference profile IDs (us.anthropic.*) are used here by default
    because they are what ClaudeCodeAgent passes to Harbor.
  EOT
  type        = list(string)
  default     = ["us.anthropic.claude-sonnet-4-5-20251001"]
}

variable "agent_model_ids" {
  description = <<-EOT
    Bedrock model IDs the agent-under-test may invoke from inside a MicroVM. Emitted PR-review tasks
    run the agent under `no-network`, so its only Bedrock path is the execution role — the model it
    reviews with must be granted here or InvokeModel 403s and the agent submits nothing. Cross-region
    inference profile IDs (us.anthropic.*); the grant scopes InvokeModel to each profile ARN and its
    underlying foundation-model ARN. Model access must also be enabled per-account in the Bedrock console.
  EOT
  type        = list(string)
  default     = ["us.anthropic.claude-sonnet-5"]
}

variable "judge_model_id" {
  description = <<-EOT
    The Bedrock model the verifier's judge invokes to adjudicate finding equivalence. Must match
    the `model` pinned in `src/eval_harvest/verifier_tpl/judge.toml`. A cross-region inference profile
    (us.anthropic.*): the execution-role grant below scopes InvokeModel to this profile ARN and to the
    underlying foundation-model ARN (the profile ID with the "us." prefix stripped), which is what
    InvokeModel authorizes against. Model access must also be enabled per-account in the Bedrock console.
  EOT
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}
