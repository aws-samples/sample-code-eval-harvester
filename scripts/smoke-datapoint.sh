#!/usr/bin/env bash
#
# smoke-datapoint.sh — the oracle/nop gradeability guard.
#
# Dispatches ONE emitted task twice on the Lambda MicroVMs environment — `--agent oracle` (submits the
# reference findings; must score non-zero coverage and exit 0) and `--agent nop` (does nothing; must
# credit nothing) — then asserts oracle ≠ nop. Neither agent needs a model or API spend; the cost is
# AWS MicroVM time only: one image build plus two short trials, ~5-10 minutes.
#
# This is NOT part of `mise run check`. It needs AWS credentials, applied Terraform, and minutes of
# wall time, so it is an explicit, on-demand acceptance gate. Run it before any change to `emit.py`,
# `verifier_tpl/`, or `candidate.py`'s patch writing — the three places whose defects a fully green
# offline gate cannot catch, because only executing a datapoint reveals them.
#
# A missing prerequisite (no Terraform outputs, no AWS creds, no `harbor`) exits NON-ZERO with a named
# reason — it never skips. "Did not run" must never read as "passed" (tech-plan.md:138).
#
# Usage:
#   scripts/smoke-datapoint.sh <emitted-task-dir>     # called by `mise run smoke-datapoint -- <dir>`
#
# Requires `terraform` on PATH and AWS credentials in the environment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${REPO_ROOT}/infrastructure/terraform"

if [[ $# -ne 1 ]]; then
  echo "usage: scripts/smoke-datapoint.sh <emitted-task-dir>" >&2
  echo "   or: mise run smoke-datapoint -- <emitted-task-dir>" >&2
  exit 2
fi
TASK_DIR="$1"

# ---------------------------------------------------------------------------
# 1. Verify terraform is available (a named prerequisite, not a skip).
# ---------------------------------------------------------------------------
if ! command -v terraform > /dev/null 2>&1; then
  echo "error: 'terraform' not found on PATH — cannot read the MicroVMs infrastructure config." >&2
  echo "  Install it from https://developer.hashicorp.com/terraform/install" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# 2. Read infrastructure outputs as JSON. `terraform output -json` returns {}
#    when not yet applied (no state) — a named prerequisite failure, not a skip.
# ---------------------------------------------------------------------------
TF_JSON="$(terraform -chdir="${TF_DIR}" output -json 2>/dev/null)"

if [[ "${TF_JSON}" == "{}" || -z "${TF_JSON}" ]]; then
  echo "error: Terraform outputs are empty — the MicroVMs infrastructure has not been deployed." >&2
  echo "  Deploy it first:  mise run infra-apply" >&2
  exit 2
fi

MICROVM_BUCKET="$(echo "${TF_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["microvm_bucket"]["value"])')"
MICROVM_BUILD_ROLE_ARN="$(echo "${TF_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["microvm_build_role_arn"]["value"])')"
MICROVM_EXECUTION_ROLE_ARN="$(echo "${TF_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["microvm_execution_role_arn"]["value"])')"

echo "smoke-datapoint: infrastructure config loaded from Terraform"
echo "  MICROVM_BUCKET             = ${MICROVM_BUCKET}"
echo "  MICROVM_BUILD_ROLE_ARN     = ${MICROVM_BUILD_ROLE_ARN}"
echo "  MICROVM_EXECUTION_ROLE_ARN = ${MICROVM_EXECUTION_ROLE_ARN}"
echo ""

# ---------------------------------------------------------------------------
# 3. Dispatch and assert. The MicroVMs environment loads by import path from the
#    installed `harvest-env` distribution, which `--group eval` syncs; the
#    guard preflights the environment's imports before any AWS call. `eval.smoke`
#    exits 0 (grades), 1 (contract failed — prints the diagnosis), or 2 (a
#    prerequisite the shell could not see, e.g. `harbor` off PATH).
# ---------------------------------------------------------------------------
cd "${REPO_ROOT}"
exec env \
  MICROVM_BUCKET="${MICROVM_BUCKET}" \
  MICROVM_BUILD_ROLE_ARN="${MICROVM_BUILD_ROLE_ARN}" \
  MICROVM_EXECUTION_ROLE_ARN="${MICROVM_EXECUTION_ROLE_ARN}" \
  uv run --group eval python -u -m eval.smoke "${TASK_DIR}"
