# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you notify AWS/Amazon Security
via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting) or
directly via email to aws-security@amazon.com. Please **do not** create a public GitHub issue.

## Sample scope and posture

**This is an experimental sample for local evaluation.** It is not intended for production workloads or
for unattended processing of third-party datasets. Use public or synthetic data and a disposable
working environment. This file is the source of truth for the posture below; the README's
[Security](README.md#security) section summarises and links here.

Two known limitations are documented rather than enforced in code. Because a coding agent driving this
CLI can rewrite any in-code guardrail, the mitigation is scope and disclosure, not a runtime guard:

- **AWS credential exposure (S1).** The optional AWS runner forwards exported AWS credentials to the
  evaluated agent. Run live evaluations only in a **dedicated, disposable AWS account** with a separate
  runner role whose permissions are limited to the sample. Do not use administrative credentials and do
  not reuse your infrastructure-provisioning session. See
  [AWS setup and credential restrictions](eval/README.md#aws-credentials-and-isolation-s1).
- **Trusted candidate inputs only (S2).** A crafted candidate can cause local files outside the dataset
  to be copied into generated tasks; candidate patch paths are not confined. Use candidates captured
  locally, keep their mechanical facts unchanged, and review generated output before sharing it.

The full release-conditions record is
[docs/security/public-release-readiness-2026-09-21.md](docs/security/public-release-readiness-2026-09-21.md).

## Suppressed infrastructure findings

The Terraform under `infrastructure/terraform/` is a **sample** provisioning stack for local
evaluation. Re-run the IaC scanner on that folder any time you change it — `mise run infra-check`
runs checkov (pinned) over the stack. A handful of checkov checks are intentionally suppressed with
inline `# checkov:skip=...` comments because they do not fit a transient, single-account eval
sample. **Review each of these before implementing this solution in production** — the trade-off that
makes them safe to skip here may not hold for your environment. In particular, this sample uses
**AWS managed keys**; using customer managed keys (CMKs) is encouraged in a production environment.

All suppressions are on the artifact bucket in `s3.tf`:

- **`CKV_AWS_18` (S3 access logging):** disabled — remove the skip and turn logging on if bucket
  access logging makes sense for your use case.
- **`CKV_AWS_145` / `CKV2_AWS_67` (S3 encryption with a CMK):** the bucket uses AWS-managed SSE-S3
  (AES256), so there is no customer managed key to require or rotate. Switch to a CMK in production if
  your key-management posture calls for it.
- **`CKV_AWS_21` (S3 versioning):** intentionally disabled — versioned objects bypass the 7-day
  lifecycle expiry and would accumulate. Enable it if you need object version history.
- **`CKV_AWS_144` (cross-region replication) and `CKV2_AWS_62` (event notifications):** omitted — the
  bucket holds ephemeral per-trial build artifacts, not durable data, so neither adds value here.

Whatever the sample suppresses, run your own security review of the infrastructure before deploying
it beyond a disposable evaluation account.

## Generative AI

This sample builds and runs evaluations with the help of generative AI, and the optional AWS runner can
incur Amazon Bedrock and other AWS charges. Generative AI can make mistakes — review generated output
**and costs** before acting on them. Evaluation results are advisory and require review before they are
used to authorize changes. See the [AWS Responsible AI Policy](https://aws.amazon.com/ai/responsible-ai/policy/).
