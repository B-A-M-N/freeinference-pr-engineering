# Runner Integration — FreeInference PR Engineering as a Runner Worker Subsystem

This document describes how FreeInference (FI) PR Engineering integrates with the
local **Runner** application (`/home/bamn/Runner`, branch `audit/runner-hardening`)
at **zero additional cost**: Runner drives scheduling, worktrees, approvals, and
completion authority; FI supplies the multi-agent engineering capability through
the existing free FreeInference access.

**Status honesty:** the FI side is implemented and tested here. The Runner side
has *not* been connected end-to-end (Runner is local and outside this sandbox);
every Runner-side statement below is either marked **VERIFIED** (read from
Runner's public GitHub `master`) or **ASSUMPTION** (exists only on the local
`audit/runner-hardening` branch, per the mission briefing). Full integration is
claimed only after both sides are connected and tested.

---

## 1. Verified vs. assumed Runner contract

### VERIFIED (read from `B-A-M-N/runner` GitHub `master`)

| Source file | Verified content |
|---|---|
| `internal/workers/workers.go` | `Adapter` interface (`Capabilities`, `Start`, `Status`, `Stream`, `Cancel`, `Collect`, `Resume`), optional `Waiter`/`ReceiptImporter`, `ErrUnsupported`, `CancelState` (acknowledged/pending/unsupported), `TaskPackage` JSON (camelCase fields), `Registry.Pick` semantics, "manual" last-resort |
| `internal/workers/command.go` | `CommandAdapter`: shell template with `{{.Worktree}}`/`{{.PackageFile}}`, writes `{artifactDir}/{attemptId}/task-package.json`, env `RUNNER_TASK_PACKAGE`/`RUNNER_TASK_ID`/`RUNNER_ATTEMPT_ID`/`RUNNER_ROLE`/`RUNNER_REPO`/`RUNNER_WORKTREE`/`RUNNER_BRANCH`/`RUNNER_BASE_SHA`/`RUNNER_MAX_MINUTES`, prompt on stdin, logs to `{artifactDir}/{attemptId}/worker.log`, HeadSHA via `git -C worktree rev-parse HEAD`, cancel = SIGTERM group → SIGKILL, roles `[implementer, verifier, planner]`, Resume unsupported |
| `internal/workers/manual.go` | `ManualAdapter`: `pending_manual` state, `assignment.txt`, `ImportReceipt` records operator-supplied headSHA+result, `Restore` after restart |
| `internal/workers/http.go` | HTTP protocol: `GET /capabilities`, `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `GET /runs/{id}/collect`, `POST /runs/{id}/resume`; capability probing at construction |
| `internal/verification/verifier.go` | Evidence model `{ID, TaskID, AttemptID, Kind: test|build|review|diff|checks|manual, Command, ExitCode, SHA, Digest, URI, Verifier, Environment, Outcome, Detail, CreatedAt}`; commands run against exact commit; `RequireHeadSHAPinning` invalidates all evidence when the candidate SHA moves; negative controls must fail; `ReviewCommand` runs against HEAD, non-zero exit = review failure |
| `internal/daemon/daemon.go` (~line 105) | Adapter registration: "manual" always; "shell" from `cfg.AgentCommand`; `cfg.Agents[]` specs with `kind` "http" → `NewHTTPAdapter` else `NewCommandAdapter` |
| `internal/config` fields | `agentCommand`, `plannerCommand`, `reviewerCommand`, `agents[{name, kind, command, baseURL, tokenEnv, projectID, timeoutMinutes}]` |
| `internal/model` | `Capabilities` (honest bools required), `Evidence` |

### ASSUMPTION (local `audit/runner-hardening` only — verify before relying)

- `internal/secureexec/executor.go`: Bubblewrap `--unshare-net` network isolation.
- `internal/scheduler/review.go`, `internal/verification/verdict.go`,
  `internal/verification/qualification.go`: independent-review and verdict
  qualification pipeline.
- `TaskPackage.VerdictFile` field and its stripping in `internal/workers/http.go`.

---

## 2. What was implemented on the FI side (this repo)

### `src/freeinference_pr/runner_bridge.py` — the compatibility adapter

- **`RunnerTaskPackage`** — pydantic mirror of Runner's verified TaskPackage JSON
  (camelCase, `worktree` required).
- **Role mapping** (no second scheduler, one FI execution per Runner task):
  | Runner role | FI specialist | Result |
  |---|---|---|
  | `planner` | `repository_analyst` | `AnalysisReport` JSON |
  | `implementer` | `implementer` | `EditsReport` → scoped edits → scoped commit → `head_sha` |
  | `verifier` | `verifier` (+ security review instructions) | verdict JSON (below) |
- **Restricted tool registries** (`build_registry`): planner/implementer get only
  `repo_list_files, repo_read_file, git_status, git_diff, git_log`; verifier adds
  `run_commands`. **All GitHub tools and approval-gated write tools are removed**:
  Runner owns GitHub and publication authority.
- **Execution scope**: edits checked against `allowedPaths`/`forbiddenPaths`
  (fnmatch globs; absolute paths and `..` traversal always rejected) plus
  `relative_to` worktree containment (symlink-safe).
- **Candidate SHA binding**: worktree HEAD must equal `baseSha` before any
  inference call; on mismatch the run fails with "candidate SHA moved" and zero
  FI calls. Result carries the resulting `head_sha` for Runner's SHA-pinned
  verification.
- **Idempotency**: results stored under `runner-result:{idempotencyKey}`;
  replaying a package returns the recorded result with `replayed=true` (retry /
  restart safety).
- **Time budget**: `maxMinutes` enforced via `asyncio.wait_for` (min 60 s);
  expiry reported honestly as `cancelled`, never success.
- **Cancellation**: `CancelledError` → state `cancelled`.
- **Persistence**: a `runner_task` job is created in the FI store keyed by
  `runner-job:{idempotencyKey}` so restarts recover state.
- **Result delivery**: result dict written to `{packageFile}.result.json`
  (redacted) for Runner collection; also returned to the caller.
- **No credentials in the task context**: `ctx.env = {}`; untrusted test
  subprocesses run under a minimal env (`_sandbox_env`) that excludes
  `FREEINFERENCE_API_KEY`, `GITHUB_TOKEN`, and everything else.
- **Verdict format** (verifier role), compatible with Runner's evidence model:
  ```json
  {"verdict": {"decision": "approve|reject", "candidate_sha": "...",
               "base_sha": "...", "diff_digest": "<sha256 of effective diff>",
               "evidence_kind": "fi_review", "assessment": "...",
               "findings": [{"title", "severity", "evidence", "detail"}],
               "missed_paths": [], "unsupported_assumptions": []}}
  ```
  `decision=reject` iff any finding has severity `critical` or `high`. This is
  review *evidence*; Runner's verifier/qualification remains the gate.

### External review handoff (no paid APIs)

- **`build_review_packet`** — complete copyable packet: repository/task identity,
  exact candidate SHA, base SHA, objective (untrusted-fenced), approved
  scope/acceptance, effective diff, real test results with exit codes, known
  risks, specific review questions, reviewer transport, and the required
  structured verdict JSON format.
- **`import_manual_review`** — records an operator-submitted verdict:
  validates `decision ∈ {approve, changes_requested, reject}`, **rejects stale
  candidate SHAs** (prior approval is invalid once the candidate changes),
  stores an `external_review` artifact with `source: "operator"`, logs
  `external_review_recorded` with the note *"operator-provided evidence, not an
  authenticated automated response"*, and ingests findings with source
  `external_review:{transport}`. Never grants publish/merge authority.

### New CLI commands

```
fi-pr runner-task <package.json>     # execute one Runner task package
fi-pr review-packet <job_id> [--out F] [--reviewer kontrol|chatgpt|gemini|mistral]
fi-pr review-import <job_id> <verdict.json> --candidate-sha SHA --reviewer R --transport T
```

### Audit corrections applied (§6 of the mission, with regression tests)

| File | Correction |
|---|---|
| `approvals.py` | Strict identity matching: a recorded approval missing any required identity binding is rejected, never stretched. Required bindings per operation; derived-operation field maps (`create_pull_request.head` ← `branch`). |
| `tools.py` | `_sandbox_env`: untrusted test subprocesses get only `PATH/HOME/PYTHONDONTWRITEBYTECODE/LANG/TERM` — no FI/GitHub credentials. `_safe_path` uses `relative_to` containment (replaces string-prefix check; symlink-safe). |
| `engine.py` | `prepare_worktree` is recovery-safe: existing checkouts are reused (branch switched, never deleted); `rm -rf` only for non-git directories. `apply_edits` uses `relative_to` containment. |
| `pyproject.toml` | `fi-pr` entry point added, reconciling documented CLI with actual scripts. |

---

## 3. What Runner already supports (no changes needed)

- **Worker abstraction and dispatch** (`workers.go`, `daemon.go`): the
  `CommandAdapter` registration path is exactly how the FI bridge plugs in.
- **Task packaging**: writes `task-package.json` and sets `RUNNER_*` env vars —
  the FI bridge consumes both.
- **SHA-pinned verification** (`verifier.go`): candidate-SHA evidence invalidation
  is Runner-side and already matches the bridge's SHA binding.
- **Manual review path** (`manual.go`): `ImportReceipt` is the natural sink for
  operator-submitted external review verdicts (Kontrol/ChatGPT/Gemini/Mistral
  packets).
- **Approvals, budgets, concurrency, leases, worktrees, GitHub reconciliation,
  CLI/TUI/web**: all remain Runner-owned; the FI bridge implements none of them.

## 4. What must change locally in Runner (exact edit notes)

All Runner-side changes are in the local `audit/runner-hardening` branch. Items
marked **(A)** depend on the unverified local-only files — confirm against the
local source first.

### 4.1 Register the FI adapter (no Go code required for the basic path)

In the Runner config (per `internal/config`), add agent specs:

```json
{"agents": [
  {"name": "fi-planner",     "kind": "command", "command": "/path/to/fi-venv/bin/fi-pr runner-task", "timeoutMinutes": 30},
  {"name": "fi-implementer", "kind": "command", "command": "/path/to/fi-venv/bin/fi-pr runner-task", "timeoutMinutes": 60},
  {"name": "fi-verifier",    "kind": "command", "command": "/path/to/fi-venv/bin/fi-pr runner-task", "timeoutMinutes": 45}
]}
```

The `CommandAdapter` writes the package to `{artifactDir}/{attemptId}/task-package.json`
and passes the path via `RUNNER_TASK_PACKAGE`; `fi-pr runner-task` accepts the
package path as its argument. Robust wiring that does not depend on template
internals:

```json
"command": "sh -c 'exec \"$FI_PR_BIN\" runner-task \"$RUNNER_TASK_PACKAGE\"'"
```

with `FI_PR_BIN=/path/to/fi-venv/bin/fi-pr` exported in Runner's environment
(or use the `{{.PackageFile}}` template field if the local template exposes it).

Set `FREEINFERENCE_API_KEY` (and optionally `FREEINFERENCE_BASE_URL`,
`FI_ORCHESTRATOR_MODEL`, `FI_IMPLEMENTER_MODEL`, `FI_REVIEWER_MODEL`) in the
environment Runner launches the adapter with. Runner's Bubblewrap sandbox
**(A)** strips the network for the *untrusted worker* process; the FI bridge
runs as the command-adapter process launched by Runner itself, so inference
works without weakening `--unshare-net` for untrusted test execution. The FI
bridge's own `_run_shell` already runs repository tests credential-free.

### 4.2 Collect the FI result (small Go change)

The bridge writes `{packageFile}.result.json` next to Runner's package file
inside Runner's artifact dir. In `internal/workers/command.go`, the `Collect`
path currently derives HeadSHA via `git rev-parse HEAD`. Add: read
`{packageFile}.result.json` if present; on parse success use its `state`,
`headSha`, `commits`, `result` (verdict/report JSON), and `error` fields, and
record the file's sha256 as the evidence `Digest`. Keep the existing
`git rev-parse HEAD` cross-check: if the collected `headSha` differs from the
worktree HEAD, fail the attempt (matching Runner's `RequireHeadSHAPinning`).
For verifier attempts, additionally ingest the verdict document from the
package's `verdictFile` via `verification.ReadReviewVerdict()` (see §4.3-bis).

### 4.3 VerdictFile secure transfer **(A — design, local-branch dependent)**

Assumed problem: `internal/workers/http.go` strips `VerdictFile` when sending
tasks to remote HTTP workers, so remote workers cannot return the validated
verdict artifact. Recommended design (works for both command and HTTP adapters):

1. Runner generates a random per-attempt verdict nonce, stores
   `sha256(expected_verdict_path)` in the attempt record, and passes only the
   *path* (not content) to the worker.
2. The worker writes the verdict to that path (command adapter: inside Runner's
   0700 artifact dir; HTTP adapter: `POST /runs/{id}/verdict`, with Runner
   verifying the nonce and the payload's `candidate_sha`).
3. On collect, Runner verifies: digest matches the recorded expectation, the
   verdict's `candidate_sha` equals the attempt's pinned SHA, and the decision
   field is well-formed. Only then is it recorded as evidence with
   `Kind: review`, `Digest`, `SHA`, `Verifier` set.
4. `http.go` can keep stripping `VerdictFile` from the TaskPackage itself; the
   verdict path travels via the collect response or a separate attempt-scoped
   endpoint, never as a worker-writable field inside the package.

This preserves the verification contract: the verdict is an artifact with a
digest and pinned SHA, not a model-generated success message.

### 4.3-bis Verdict document contract (source-verified by the local audit)

The hardening branch's verifier packages carry `baseSha` (task baseline),
`candidateSha` (exact candidate under review), `verdictFile` (where the
independent-review decision must be written), `readOnly`,
`maxDurationSeconds`, and `maxTokens`. The FI bridge now implements all of
them:

- Verifier packages bind the worktree HEAD to `candidateSha` (fallback:
  `baseSha` for master-style packages); implementer/planner bind to `baseSha`.
- The effective diff is `git diff baseSha..candidateSha` when the two differ.
- When `verdictFile` is set, the bridge writes Runner's flat document:
  `{"decision", "sha": <candidate>, "evidence": [{"uri", "digest"}]}` where
  `digest` is the SHA-256 of the diff artifact written at `<verdictFile>.diff`.
  Runner should recompute that digest itself — FI's digest is advisory.
- `readOnly` refuses implementer edits; `maxTokens` aborts on exhaustion;
  `maxDurationSeconds` tightens the time budget; an empty `allowedPaths`
  denies every write (deny-by-default).
- The FI verdict carries `reviewer_provenance: fi_self_review` and
  `grants_publication_authority: false`; it must not satisfy the
  independent-review qualification gate on its own.

**Runner-side note**: the hardening `CommandAdapter` must mount/allow the
`verdictFile` destination inside the sandbox (see §4.6 correction), and
`verification.ReadReviewVerdict()` should validate `sha` against the attempt's
pinned candidate SHA and each evidence digest against the referenced artifact.

### 4.4 External review ingestion

Map the FI `review-import` record onto Runner's `ManualAdapter.ImportReceipt`
**(VERIFIED interface)**: the operator pastes the returned verdict JSON into
Runner (or a small Runner-side subcommand reads the file produced by
`fi-pr review-import`), supplying `headSHA` = verdict `candidate_sha` and the
result payload. Runner's qualification policies **(A)** then decide whether the
operator-provided review satisfies the review gate. Label it as
operator-provided, non-authenticated evidence.

### 4.5 Tests Runner should add

- Command-adapter run against a fixture package whose command is
  `fi-pr runner-task` with a stub FI client (the FI repo's
  `tests/test_runner_bridge.py` fixtures can be mirrored in Go with golden
  package/result JSON).
- Collect parses `{packageFile}.result.json`; SHA cross-check fails on mismatch.
- Verdict-file digest + candidate-SHA validation (§4.3).
- Manual import of an external verdict records evidence with
  `Verifier: "operator"` and does not satisfy publish authority by itself.

### 4.6 Security boundaries to preserve

- `--unshare-net` stays for untrusted work **(A)**. **Corrected after the local
  audit**: when sandboxing is enabled, Runner's `CommandAdapter` runs the FI
  executable itself inside Bubblewrap — so FI inference DOES run inside the
  sandbox namespace, which disables networking and does not mount the FI venv,
  FI state directory, or the result/verdict-file destinations. The earlier
  claim that FI inference executes outside the sandbox was wrong. Resolutions:
  (a) Runner-side bind-flags for exactly the FI venv, state dir, and
  per-attempt artifact paths (no general network), or (b) an HTTP worker
  reached through a loopback-only path, or (c) keep network isolation and run
  FI inference in a Runner-side broker the sandbox talks to over a mounted
  Unix socket. Until one of these ships, sandboxed FI workers cannot reach the
  inference endpoint and the harness result below does NOT exercise Bubblewrap.
- FI bridge gets no GitHub credentials in Runner mode (`ctx.env = {}`).
- No auto-merge; external/automated review never grants publish authority.
- Idempotency keys prevent duplicate runs on retry/restart; never let both
  Runner and FI manage the same job (FI standalone mode and Runner-managed mode
  are separate entry points — a package executed via `runner-task` is Runner's).

## 5. Review connection classification

| Reviewer | Status | Mechanism |
|---|---|---|
| FI verifier (free FreeInference) | **Automatic** (FI-side implemented; Runner-side pending) | `runner-task` verifier role → verdict JSON → Runner evidence |
| ChatGPT via Kontrol | **Manual/assisted** | `fi-pr review-packet` → paste into the existing Kontrol review rendezvous (MCP tools `submit_for_review` / `provide_review_feedback` / `await_review_feedback` exist and were verified in the Kontrol source) → paste verdict back via `fi-pr review-import` / Runner `ImportReceipt`. Automatic forwarding would require a Runner-side MCP client with worker/reviewer principals — a Runner-side change; do not implement blindly. |
| Gemini | **Manual** | review packet into an existing user-facing session; verdict pasted back. No paid API. |
| Mistral | **Manual** | same as Gemini (user-facing session / Vibe chat). No paid API. |
| Any paid model API | **Unavailable by constraint** | not integrated; never silently substituted |

## 6. Tests passed (this repo, mocked FI — real-FI smoke not run here: no key in sandbox)

- Full Python suite: **119 passed** (`pytest -q`), including:
  - `tests/test_runner_bridge.py` (32): role mapping (no GitHub tools), registry restriction, honest unsupported-role failure, non-git worktree rejection, **candidate SHA binding with zero FI calls on stale candidate**, scoped edits + commit + `head_sha`, allowed/forbidden path enforcement, idempotent replay (result file rewritten with `replayed: true`), verifier verdict (decision / candidate_sha / 64-hex diff_digest / evidence_kind / `fi_self_review` provenance), implementer denied `run_commands`, symlinked-target edit rejection, bounded manual-review ingestion, oversized-submission rejection, verifier diff fallback to trunk merge-base, blocker-based rejection, cancellation → `cancelled`, runner job persisted for restart recovery, path globs, package roundtrip, review packet contents, manual review import (operator evidence + findings + event note), stale-candidate rejection, malformed verdict rejection, result-file Runner `Collect` schema.
  - `tests/test_integration_regressions.py` (13): strict approval identity matching, credential-free sandbox env, credential-free untrusted command execution, `..` traversal rejection, symlink escape rejection, non-destructive worktree recovery.
  - All pre-existing Mission-2 tests still pass.
- Go contract suite: **9/9 subtests pass** through Runner's real `CommandAdapter`
  (see §8) — including a hardening-style verifier package (candidateSha +
  verdictFile + readOnly + budgets) validated end-to-end.
  **Caveat**: the harness runs outside Bubblewrap, so it validates the
  adapter/bridge contract, not production sandbox behavior (see §4.6).

## 7. What remains unverified

1. **Real FreeInference inference through the bridge** — the sandbox has no
   `FREEINFERENCE_API_KEY`. Run the live qualification on the user's machine:
   `scripts/runner_qualification_live.sh` (disposable repo, planner →
   implementer → verifier against real FI, no PR/push), or the Go harness in
   live mode: `FI_LIVE=1 RUNNER_SRC=/home/bamn/Runner ./integration/runner/run_contract_tests.sh`.
2. **Runner-side result ingestion** — Runner's `Collect` does not yet read
   `{packageFile}.result.json`; the §4.2 Go change is still required on the
   local hardening branch. The Go harness validates the result-file schema and
   the adapter execution path, but not Runner's ingestion of it.
3. **Local-branch Runner internals** — `secureexec` Bubblewrap flags,
   `review.go`, `verdict.go`, `qualification.go`, and the `VerdictFile` field are
   assumptions from the mission briefing; verify against the local checkout
   before implementing §4.3.
4. **Kontrol automatic forwarding** — the MCP rendezvous tools exist (verified in
   Kontrol source) but an MCP client session with worker/reviewer principals is
   required; until then the flow is operator-mediated.
5. **HTTP-worker mode of the bridge** — not implemented (the command adapter is
   the integration path); do not advertise `POST /runs` capabilities.
6. **Runner qualification machinery end-to-end** — the harness exercises the
   adapter contract, not Runner's `scheduler/review.go` → `verification/`
   pipeline; that requires the local hardening branch.
7. **Sandboxed execution** — the harness runs outside Bubblewrap; whether FI
   workers can reach the inference endpoint and write the result/verdict files
   inside the hardening sandbox is untested (needs the Runner-side mounts or
   broker from §4.6).
8. **Budget accounting in Runner** — FI reports token usage in `Collect.usage`,
   but Runner-side accumulation/enforcement against `maxTokens`/`budgetUsd` is
   a Runner-side change and is unverified.

## 8. Mission 4 — integration qualification (Go contract harness)

### 8.1 Verified compatibility matrix

| Contract element | Runner (GitHub `master`, verified) | FI bridge | Status |
|---|---|---|---|
| `TaskPackage` JSON | 23 camelCase fields (`internal/workers/workers.go`) | `RunnerTaskPackage` mirrors all 23 | ✅ verified via roundtrip + Go harness |
| nil slices in Go JSON | marshaled as `null` | accepted → `[]` (`_null_list_to_empty` validator) | ✅ bug found by harness, fixed |
| Dispatch | `sh -c` template, `cmd.Dir = pkg.Worktree`, all placeholders shell-quoted, package at `{artifactDir}/{attemptId}/task-package.json` | `fi-pr runner-task {{.PackageFile}}` | ✅ harness passes 8/8 |
| Roles | `[implementer, verifier, planner]` | exact mapping, others fail honestly | ✅ |
| Result | `Collect` does **not** read a result file (master) | writes `{packageFile}.result.json` in `Collect` JSON schema (camelCase, `runId/state/headSha/commits/usage{usd,known,tokens,detail}/error` + additive `taskId/attemptId/replayed`) | ✅ schema verified; Runner-side ingestion (§4.2) pending |
| Idempotency | `IdempotencyKey` in package | replay returns prior result with `replayed: true`, no re-inference | ✅ harness: duplicate task → ≤1 FI call |
| Cancel | SIGTERM process group, SIGKILL after 5s | subprocess terminates → state `cancelled` | ✅ harness |
| Deadline | `context.WithTimeout` → failed "worker exceeded its time budget" | honest failure, no fake success | ✅ harness |
| Candidate SHA | master: `baseSha`. Hardening (audit-verified): `baseSha` = task baseline, `candidateSha` = exact candidate; verifier binds to `candidateSha` | verifier binds HEAD to `candidateSha` (fallback `baseSha`); implementer/planner bind to `baseSha`; stale candidate → zero inference, error `candidate SHA moved` | ✅ harness + pytest |
| Verifier diff | hardening: `baseSha`..`candidateSha` | exact range diff when the SHAs differ; merge-base/parent fallback for master-style packages | ✅ harness + pytest |
| Verdict document | `verdictFile` + `verification.ReadReviewVerdict()` expecting `{decision, sha, evidence:[{uri,digest}]}` | flat document written to `verdictFile`; diff artifact at `<verdictFile>.diff`; SHA-256 digest of the artifact | ✅ harness + pytest (Runner-side ingestion still pending) |
| `readOnly` / `maxDurationSeconds` / `maxTokens` | hardening package fields | readOnly refuses implementer edits; duration tightens the time budget; token budget aborts on exhaustion | ✅ pytest |
| Path scope | hardening: empty allowlist must not be permissive | empty `allowedPaths` denies every write; forbidden globs win; traversal/symlink escapes rejected | ✅ pytest |
| Commit isolation | previously staged index content must not enter the worker's commit | pathspec commit of exactly the applied paths; post-commit verification fails on any extra path | ✅ pytest |
| Verifier diff | verifier packages carry candidate SHA as `baseSha` (empty `git diff baseSha` is the *correct* case) | `_candidate_diff` fallback chain: `git diff baseSha` → trunk merge-base → parent commit | ✅ fixed (3rd discrepancy) |
| Review provenance | FI self-review must not qualify as external review | verdict carries `reviewer_provenance: fi_self_review`, `grants_publication_authority: false`; manual imports carry `provenance: operator_provided` | ✅ implemented + tested |
| Evidence ingestion | bounded, schema-validated | 1 MB submission bound; findings capped (50 findings × 20 evidence × 4000 chars) | ✅ tested |

### 8.2 Discrepancies found by the real Go harness (and fixed)

1. **Go nil slices → JSON `null`**: pydantic `list[str]` fields rejected `null`
   → every task failed validation. Fixed with a `mode="before"` field validator
   on `acceptance/nonGoals/allowedPaths/forbiddenPaths/evidence`.
2. **Replay skipped the result file**: the idempotent-replay path returned the
   prior result without re-running `_finish`, so the `Collect` result file was
   never rewritten with `replayed: true`. Fixed.
3. **Verifier packages carry the candidate SHA as `baseSha`**: `git diff
   baseSha` is empty exactly when the binding is correct, so the verifier saw a
   zero-length diff. Fixed with `_candidate_diff` (merge-base/parent fallbacks;
   missing trunk branches are skipped, not fatal).

### 8.3 Harness usage

```bash
# mocked-FI contract tests through Runner's real CommandAdapter
# (copies RUNNER_SRC to a temp dir; never modifies the original)
RUNNER_SRC=/home/bamn/Runner ./integration/runner/run_contract_tests.sh
FI_KEEP_WORK=1   # preserve the temp Runner copy

# live FreeInference model through the same harness (needs the key)
FI_LIVE=1 RUNNER_SRC=/home/bamn/Runner ./integration/runner/run_contract_tests.sh

# standalone live qualification (disposable repo, no PR, no push)
FREEINFERENCE_API_KEY=... scripts/runner_qualification_live.sh
```

### 8.4 Phase-2 boundary notes

- Runner owns the authoritative candidate SHA; FI's `diff_digest` is
  **advisory** — Runner must independently compute the candidate diff digest
  (§4.2) and treat adapter-produced evidence as claims, never as approval or
  publication authority.
- Untrusted work stays credential-free (`tools.py` sandbox env) and network-
  isolated on the Runner side (`--unshare-net`); FI inference runs in the
  adapter process only.
- Scope enforcement is re-validated against traversal, symlink escapes, and
  disallowed/forbidden paths at apply time (tested).

### 8.5 Local-audit corrections (round 2)

The operator's local audit of `audit/runner-hardening` (HEAD `628c277`) found
contract divergences; the FI-side fixes:

1. **Hardening package fields** — `candidateSha`, `verdictFile`, `readOnly`,
   `maxDurationSeconds`, `maxTokens` were missing; now parsed (null-tolerant)
   and enforced.
2. **Verifier SHA binding** — was pinned to `baseSha` (which hardening uses for
   the task baseline); now binds to `candidateSha` with `baseSha` fallback.
3. **Flat verdict document** — FI's nested verdict did not satisfy
   `ReadReviewVerdict()`; the bridge now also writes the flat document to
   `verdictFile` with a verifiable diff-artifact digest.
4. **Sandbox reality** — corrected the false claim that FI inference runs
   outside the Bubblewrap namespace (see §4.6); sandboxed FI workers currently
   cannot reach the inference endpoint without a Runner-side mount/socket
   change.
5. **Empty allowlist** — was allow-all; now deny-by-default.
6. **Commit isolation** — previously staged index content could enter the
   FI commit; commits are now pathspec-scoped and verified.
7. **Publication SHA fidelity (standalone mode)** — `_publish_pr` reconstructed
   the remote commit via the Git data API, producing a different SHA than the
   verified candidate; it now pushes the exact verified commit and aborts
   unless `ls-remote` confirms the remote branch head equals the candidate SHA.
   Token transport uses an env-based credential helper (never argv/URL).

