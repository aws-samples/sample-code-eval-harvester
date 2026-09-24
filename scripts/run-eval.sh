#!/usr/bin/env bash
#
# run-eval.sh — Run the core-bet eval, pulling execution infrastructure from
# Terraform outputs so no env vars need to be set manually.
#
# Checks that the infrastructure has been deployed (terraform apply) before
# running; errors with a clear deployment command if it hasn't.
#
# Usage:
#   scripts/run-eval.sh [<eval args>]         # called by mise run eval
#
# Args are forwarded to eval.run, e.g.:
#   mise run eval -- -m us.anthropic.claude-sonnet-4-5-20251001
#
# Requires `terraform` on PATH and short-lived credentials for the dedicated
# sample runner role. See eval/README.md: the agent receives exported credentials (S1).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${REPO_ROOT}/infrastructure/terraform"

# ---------------------------------------------------------------------------
# 1. Verify terraform is available.
# ---------------------------------------------------------------------------
if ! command -v terraform > /dev/null 2>&1; then
  echo "error: 'terraform' not found on PATH." >&2
  echo "  Install it from https://developer.hashicorp.com/terraform/install" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Read infrastructure outputs as JSON.
#    `terraform output -json` returns {} when not yet applied (no state).
#    We check for that before trying to extract individual values.
# ---------------------------------------------------------------------------
TF_JSON="$(terraform -chdir="${TF_DIR}" output -json 2>/dev/null)"

if [[ "${TF_JSON}" == "{}" || -z "${TF_JSON}" ]]; then
  echo "error: Terraform outputs are empty — the infrastructure has not been deployed yet." >&2
  echo "" >&2
  echo "  Deploy it first:" >&2
  echo "    Follow infrastructure/terraform/README.md, then run mise run infra-apply." >&2
  echo "    Set eval_caller_principal_arns to a separate sample runner role." >&2
  echo "    Provision with another identity; do not reuse provisioning credentials for eval." >&2
  exit 1
fi

MICROVM_BUCKET="$(echo "${TF_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["microvm_bucket"]["value"])')"
MICROVM_BUILD_ROLE_ARN="$(echo "${TF_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["microvm_build_role_arn"]["value"])')"
MICROVM_EXECUTION_ROLE_ARN="$(echo "${TF_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["microvm_execution_role_arn"]["value"])')"

echo "eval: infrastructure config loaded from Terraform"
echo "  MICROVM_BUCKET             = ${MICROVM_BUCKET}"
echo "  MICROVM_BUILD_ROLE_ARN     = ${MICROVM_BUILD_ROLE_ARN}"
echo "  MICROVM_EXECUTION_ROLE_ARN = ${MICROVM_EXECUTION_ROLE_ARN}"
echo ""

# ---------------------------------------------------------------------------
# 3. Run the eval with the infrastructure env vars set.
#    CLAUDE_CODE_USE_BEDROCK tells the agent to use the Bedrock client.
#    The MicroVMs environment is loaded by import path from the installed
#    `harvest-env` distribution (harvest_env/), which `--group eval` syncs —
#    there is no PYTHONPATH overlay: the `harbor.*` namespace belongs to the
#    installed Harbor and cannot be partially overlaid by a same-named tree.
#    `eval.run` preflights the environment's imports before any AWS call.
#
#    Gotcha: do NOT reach for `uv run --with <pkg> harbor …` to patch a missing
#    dependency in. `--with` overlays the interpreter, but the `harbor` console
#    script's shebang points at .venv/bin/python and bypasses the overlay, so the
#    dependency looks uninstalled when it is not. Sync the `eval` group instead.
# ---------------------------------------------------------------------------
cd "${REPO_ROOT}"
printf '%s\n' \
  "sample warning (S1): exported AWS credentials are forwarded to the evaluated agent." \
  "Use short-lived credentials for a restricted runner role in a dedicated disposable account." \
  "Account isolation and permissions are not checked here; see eval/README.md." >&2
exec env \
  MICROVM_BUCKET="${MICROVM_BUCKET}" \
  MICROVM_BUILD_ROLE_ARN="${MICROVM_BUILD_ROLE_ARN}" \
  MICROVM_EXECUTION_ROLE_ARN="${MICROVM_EXECUTION_ROLE_ARN}" \
  CLAUDE_CODE_USE_BEDROCK=1 \
  uv run --group eval python -m eval.run "$@"
