#!/usr/bin/env bash
# Live FreeInference qualification for the Runner bridge.
#
# Runs on a machine where FREEINFERENCE_API_KEY is set (e.g. the Runner host).
# Creates a DISPOSABLE local git repository, dispatches planner / implementer /
# verifier task packages through the real bridge against the real
# FreeInference endpoint, and validates Runner-compatible evidence — without
# creating any PR, pushing anything, or touching GitHub.
#
# Usage:
#   export FREEINFERENCE_API_KEY=hyi-...        # required
#   ./scripts/runner_qualification_live.sh [model]
#
# Optional environment:
#   FREEINFERENCE_BASE_URL   default https://freeinference.org/v1
#   FI_IMPLEMENTER_MODEL / FI_ANALYST_MODEL / FI_VERIFIER_MODEL  model overrides
#   RUNNER_SRC + FI_LIVE=1   instead drive Runner's real Go CommandAdapter:
#     RUNNER_SRC=/home/bamn/Runner FI_LIVE=1 ./integration/runner/run_contract_tests.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FI_REPO="$(cd "$HERE/.." && pwd)"
FI_PR="${FI_PR_BIN:-$FI_REPO/.venv/bin/fi-pr}"

if [[ -z "${FREEINFERENCE_API_KEY:-}" ]]; then
  echo "error: FREEINFERENCE_API_KEY is not set" >&2
  exit 2
fi
if [[ ! -x "$FI_PR" ]]; then
  echo "error: fi-pr not found at $FI_PR (run scripts/bootstrap.sh first)" >&2
  exit 2
fi

WORK="$(mktemp -d /tmp/fi-live-qual.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
echo "== Live FreeInference qualification (disposable repo: $WORK) =="

git init -q -b main "$WORK/wt"
git -C "$WORK/wt" config user.email qualification@local
git -C "$WORK/wt" config user.name "FI Qualification"
cat > "$WORK/wt/src.py" <<'EOF'
VALUE = 1
EOF
echo "X = 1" > "$WORK/wt/other.py"
git -C "$WORK/wt" add -A
git -C "$WORK/wt" commit -q -m "base"
BASE_SHA="$(git -C "$WORK/wt" rev-parse HEAD)"
mkdir -p "$WORK/artifacts"

make_package() {  # role goal allowed idem
  local role="$1" goal="$2" allowed="$3" idem="$4"
  local allowed_json="[]"
  [[ -n "$allowed" ]] && allowed_json="[\"$allowed\"]"
  cat > "$WORK/artifacts/$idem.json" <<EOF
{
  "taskId": "live-$idem", "attemptId": "live-$idem", "attemptNo": 1,
  "projectId": "qualification", "repository": "local/disposable",
  "role": "$role", "title": "live qualification", "goal": "$goal",
  "instructions": "Keep the change minimal and in scope.",
  "acceptance": ["VALUE == 2"], "nonGoals": [],
  "baseSha": "$BASE_SHA", "branch": "runner/live-qual",
  "worktree": "$WORK/wt", "allowedPaths": $allowed_json, "forbiddenPaths": [],
  "maxMinutes": 10, "budgetUsd": 0, "evidence": [],
  "outputSchema": "", "stopBehavior": "",
  "idempotencyKey": "live-$idem", "packageFile": "$WORK/artifacts/$idem.json"
}
EOF
}

run_role() {  # role goal allowed idem
  local role="$1" goal="$2" allowed="$3" idem="$4"
  make_package "$role" "$goal" "$allowed" "$idem"
  echo "--- $role ---"
  if "$FI_PR" runner-task "$WORK/artifacts/$idem.json"; then
    echo "[$role] exit=0 (succeeded)"
  else
    echo "[$role] exit=$? (failed — see error in result file)"
  fi
  python3 - "$WORK/artifacts/$idem.json.result.json" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
print(f"  state={data['state']} headSha={data['headSha'][:12]} commits={len(data['commits'])} error={data['error'][:120]}")
PYEOF
}

run_role planner     "Analyze src.py and plan how VALUE should become 2" ""            planner
run_role implementer "Change VALUE from 1 to 2 in src.py"               "src.py"       implementer

NEW_SHA="$(git -C "$WORK/wt" rev-parse HEAD)"
if [[ "$NEW_SHA" == "$BASE_SHA" ]]; then
  echo "implementer produced no commit; skipping verifier step" >&2
else
  make_package verifier "Review the candidate change against acceptance: VALUE == 2" "" verifier
  # The verifier package must carry the NEW candidate SHA as baseSha so the
  # bridge binds to the candidate, not the original base.
  python3 - "$WORK/artifacts/verifier.json" "$NEW_SHA" <<'PYEOF'
import json, sys
path, sha = sys.argv[1], sys.argv[2]
pkg = json.load(open(path))
pkg["baseSha"] = sha
json.dump(pkg, open(path, "w"), indent=2)
PYEOF
  echo "--- verifier ---"
  if "$FI_PR" runner-task "$WORK/artifacts/verifier.json"; then
    echo "[verifier] exit=0 (succeeded)"
  else
    echo "[verifier] exit=$? (failed — see error in result file)"
  fi
  python3 - "$WORK/artifacts/verifier.json.result.json" "$NEW_SHA" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
verdict = json.loads(data["result"]).get("verdict", {}) if data["result"] else {}
print(f"  state={data['state']}")
print(f"  decision={verdict.get('decision')} candidate_sha={verdict.get('candidate_sha','')[:12]}"
      f" provenance={verdict.get('reviewer_provenance')} publication_authority={verdict.get('grants_publication_authority')}")
ok = verdict.get("candidate_sha") == sys.argv[2]
print(f"  candidate identity binding: {'OK' if ok else 'MISMATCH'}")
PYEOF
fi

echo
echo "== Qualification complete. Nothing was pushed; no PR was created. =="
echo "Evidence files: $WORK was temporary; re-run with FI_KEEP_WORK=1 to keep artifacts."
