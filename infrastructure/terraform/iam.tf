data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Build role — the Lambda MicroVMs platform assumes this during image builds.
# The caller passes its ARN via MICROVM_BUILD_ROLE_ARN; the platform uses it
# to read the build artifact from S3, write CloudWatch logs, and publish the
# Firecracker snapshot.
#
# NOTE: the trust principal is "lambda.amazonaws.com", not
# "lambda-microvms.amazonaws.com". Lambda MicroVMs is not its own IAM service:
# the API model (botocore lambda-microvms/2025-09-09) has endpointPrefix and
# signingName both set to "lambda", and IAM's CreateRole rejects every
# "*microvms*" spelling with MalformedPolicyDocument / Invalid principal.
# Verified against IAM: only lambda.amazonaws.com is accepted.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "microvm_build" {
  name        = "${var.project}-microvm-build"
  description = "Assumed by the Lambda MicroVMs platform during Firecracker image builds"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaMicroVMsTrust"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project = var.project
  }
}

resource "aws_iam_policy" "microvm_build" {
  name        = "${var.project}-microvm-build"
  description = "Permissions for the Lambda MicroVMs build role"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ArtifactAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.microvm_artifacts.arn,
          "${aws_s3_bucket.microvm_artifacts.arn}/*",
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:CreateLogDelivery",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda-microvms/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "microvm_build" {
  role       = aws_iam_role.microvm_build.name
  policy_arn = aws_iam_policy.microvm_build.arn
}

# ---------------------------------------------------------------------------
# Execution role — assumed by the running MicroVM if the task requests AWS
# access from inside the VM. Optional for the current eval objectives
# (pydantic-13611-reject and pydantic-13731-approve make no AWS calls), but
# pre-provisioned so MICROVM_EXECUTION_ROLE_ARN can be set if needed.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "microvm_execution" {
  name        = "${var.project}-microvm-execution"
  description = "Assumed by running Lambda MicroVMs when the task requests AWS access"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaMicroVMsTrust"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project = var.project
  }
}

resource "aws_iam_policy" "microvm_execution" {
  name        = "${var.project}-microvm-execution"
  description = "Minimal permissions for a running Lambda MicroVM"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda-microvms/*"
      },
      {
        # The verifier's judge runs *inside* the MicroVM under this execution role and calls Bedrock
        # (score.py, LiveJudge → converse) to adjudicate finding equivalence. Without this the
        # judge AccessDenies and every graded trial scores coverage_all = 0. The judge model is a
        # cross-region inference profile (us.anthropic.*); InvokeModel authorizes against the profile
        # ARN *and* the underlying foundation-model ARN (the profile ID minus its "us." prefix), across
        # every region the profile may route to (region wildcard). Scoped to that one model — no "*".
        Sid    = "BedrockJudge"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = [
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${var.judge_model_id}",
          "arn:aws:bedrock:*::foundation-model/${replace(var.judge_model_id, "us.", "")}",
        ]
      },
      {
        # The agent-under-test runs *inside* the MicroVM under this execution role too: emitted
        # PR-review tasks seal the agent env at `no-network`, so its only Bedrock path is this role
        # (host/caller credentials injected via --agent-env are ignored — the SDK falls back to the
        # instance role). Without this grant the reviewer 403s on InvokeModelWithResponseStream and
        # submits no findings, scoring a false coverage_all = 0. Scoped per model to the profile ARN
        # and its underlying foundation-model ARN (profile ID minus the "us." prefix), region wildcard
        # for cross-region routing — no "*" resource.
        Sid    = "BedrockAgent"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = flatten([
          for model_id in var.agent_model_ids : [
            "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${model_id}",
            "arn:aws:bedrock:*::foundation-model/${replace(model_id, "us.", "")}",
          ]
        ])
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "microvm_execution" {
  role       = aws_iam_role.microvm_execution.name
  policy_arn = aws_iam_policy.microvm_execution.arn
}

# ---------------------------------------------------------------------------
# Caller policy — the documented sample setup explicitly targets a separate
# runner role via eval_caller_principal_arns (S1; see README.md). The legacy
# empty-list fallback attaches to the Terraform caller; it does not narrow
# that identity's existing permissions.
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "eval_caller" {
  name        = "${var.project}-eval-caller"
  description = "Grants a principal the permissions needed to run mise run eval"

  # The document is written inline with jsonencode() rather than loaded via templatefile(): a
  # templatefile() reference is an opaque string to the IAM static analyzers (checkov, tfsec), so
  # an external policy file drops these statements out of scanner scope — including the no-"*"-
  # Resource rule. Inline HCL keeps every Action/Resource in view of the scanners. Each Resource is
  # scoped to a concrete ARN; no statement uses "*".
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ArtifactReadWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.microvm_artifacts.arn,
          "${aws_s3_bucket.microvm_artifacts.arn}/*",
        ]
      },
      {
        Sid    = "PassBuildAndExecutionRoles"
        Effect = "Allow"
        # nosemgrep: no-iam-resource-exposure -- iam:PassRole is scoped to exactly the two role ARNs
        # below (never "*"), so the caller can pass only these roles and cannot escalate to an
        # arbitrary one. PassRole is required: the Lambda MicroVMs API is passed the build role for
        # server-side image builds and the execution role as the VM's runtime identity. The rule is
        # action-based and fires on iam:PassRole regardless of Resource scoping.
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.microvm_build.arn,
          aws_iam_role.microvm_execution.arn,
        ]
      },
      {
        Sid    = "LambdaMicroVMsAPI"
        Effect = "Allow"
        Action = [
          "lambda:CreateMicrovmAuthToken",
          "lambda:CreateMicrovmImage",
          "lambda:CreateMicrovmShellAuthToken",
          "lambda:DeleteMicrovmImage",
          "lambda:DeleteMicrovmImageVersion",
          "lambda:GetMicrovm",
          "lambda:GetMicrovmImage",
          "lambda:GetMicrovmImageBuild",
          "lambda:GetMicrovmImageVersion",
          "lambda:ListManagedMicrovmImageVersions",
          "lambda:ListManagedMicrovmImages",
          "lambda:ListMicrovmImageBuilds",
          "lambda:ListMicrovmImageVersions",
          "lambda:ListMicrovmImages",
          "lambda:ListMicrovms",
          "lambda:ListTags",
          "lambda:TagResource",
          "lambda:UntagResource",
          "lambda:ResumeMicrovm",
          "lambda:RunMicrovm",
          "lambda:SuspendMicrovm",
          "lambda:TerminateMicrovm",
          "lambda:UpdateMicrovmImage",
          "lambda:UpdateMicrovmImageVersion",
        ]
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:microvm-image:*",
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:microvm:*",
        ]
      },
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = [
          for model_id in var.eval_model_ids : "arn:aws:bedrock:*::foundation-model/${model_id}"
        ]
      }
    ]
  })
}

# The sample setup sets eval_caller_principal_arns to a dedicated runner role.
# The caller policy attaches to IAM roles only — never directly to an IAM user.
# Attaching a managed policy to a user is the CKV_AWS_40 anti-pattern and runs
# counter to the S1 guidance (dedicated runner role, temporary credentials, no
# long-lived user keys; see eval/README.md). The empty-list fallback therefore
# targets the Terraform caller only when it is an assumed role.
locals {
  caller_arn = data.aws_caller_identity.current.arn

  # An assumed-role session ARN (arn:aws:sts::acct:assumed-role/RoleName/session)
  # cannot be a policy target; the underlying role name is what attaches.
  caller_is_assumed_role = can(regex(":assumed-role/", local.caller_arn))
  caller_role_name       = local.caller_is_assumed_role ? split("/", local.caller_arn)[1] : null

  override_principals = length(var.eval_caller_principal_arns) > 0

  # When overriding, take the role names from the supplied role ARNs (user ARNs
  # are rejected by the variable's validation). When not, fall back to the
  # detected caller identity only if it is an assumed role.
  attach_role_names = local.override_principals ? [
    for arn in var.eval_caller_principal_arns :
    element(split("/", arn), length(split("/", arn)) - 1) if can(regex(":role/", arn))
  ] : (local.caller_is_assumed_role ? [local.caller_role_name] : [])
}

resource "aws_iam_role_policy_attachment" "eval_caller" {
  for_each   = toset(local.attach_role_names)
  role       = each.value
  policy_arn = aws_iam_policy.eval_caller.arn
}
