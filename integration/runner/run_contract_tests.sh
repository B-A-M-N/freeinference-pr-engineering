#!/usr/bin/env bash
# Run the FreeInference bridge contract tests through Runner's REAL
# CommandAdapter Go code — without modifying Runner.
#
# Usage:
#   RUNNER_SRC=/home/bamn/Runner FI_PR_BIN=/path/.venv/bin/fi-pr \
#     ./integration/runner/run_contract_tests.sh
#
# - RUNNER_SRC: Runner checkout to test against (default: $HOME/Runner, then
#   a sibling github__B-A-M-N__runner clone). The checkout is COPIED to a temp
#   directory; the original is never modified.
# - FI_PR_BIN:  fi-pr executable (default: <this repo>/.venv/bin/fi-pr).
#
# The mock FI endpoint is local-only. For a live-model pass set FI_LIVE=1 and
# FREEINFERENCE_API_KEY (uses https://freeinference.org/v1).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FI_REPO="$(cd "$HERE/../.." && pwd)"
RUNNER_SRC="${RUNNER_SRC:-}"
FI_PR_BIN="${FI_PR_BIN:-$FI_REPO/.venv/bin/fi-pr}"

if [[ -z "$RUNNER_SRC" ]]; then
  for candidate in "$HOME/Runner" "$FI_REPO/../github__B-A-M-N__runner"; do
    if [[ -d "$candidate/internal/workers" ]]; then RUNNER_SRC="$(cd "$candidate" && pwd)"; break; fi
  done
fi
if [[ -z "$RUNNER_SRC" || ! -d "$RUNNER_SRC/internal/workers" ]]; then
  echo "error: set RUNNER_SRC to your Runner checkout (contains internal/workers)" >&2
  exit 2
fi
if [[ ! -x "$FI_PR_BIN" ]]; then
  echo "error: fi-pr not found at $FI_PR_BIN (run scripts/bootstrap.sh)" >&2
  exit 2
fi

WORK="$(mktemp -d /tmp/fi-runner-contract.XXXXXX)"
trap '[[ "${FI_KEEP_WORK:-0}" == "1" ]] || rm -rf "$WORK"' EXIT
cp -r "$RUNNER_SRC" "$WORK/runner"
rm -rf "$WORK/runner/.git"
cp "$HERE/fi_contract_test.go" "$WORK/runner/internal/workers/fi_contract_test.go"

export FI_PR_BIN
export RUNNER_TEST_WORKDIR="$WORK"
cd "$WORK/runner"

if [[ "${FI_LIVE:-0}" == "1" ]]; then
  if [[ -z "${FREEINFERENCE_API_KEY:-}" ]]; then
    echo "error: FI_LIVE=1 requires FREEINFERENCE_API_KEY" >&2; exit 2
  fi
  echo "== LIVE qualification against ${FREEINFERENCE_BASE_URL:-https://freeinference.org/v1} =="
  go test ./internal/workers/ -run TestFIBridgeContract -v -count=1 "$@"
else
  echo "== Contract qualification (mock FI endpoint, real CommandAdapter + real bridge) =="
  go test ./internal/workers/ -run TestFIBridgeContract -v -count=1 "$@"
fi
