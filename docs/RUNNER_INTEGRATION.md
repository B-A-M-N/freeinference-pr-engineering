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
`head_sha`, `commits`, `result` (verdict/report JSON), and `error` fields, and
record the file's sha256 as the evidence `Digest`. Keep the existing
`git rev-parse HEAD` cross-check: if the collected `head_sha` differs from the
worktree HEAD, fail the attempt (matching Runner's `RequireHeadSHAPinning`).

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

- `--unshare-net` stays for untrusted work **(A)**; FI inference runs in the
  adapter process, not inside untrusted sandboxes.
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

- Full suite: **105 passed** (`pytest -q`), including:
  - `tests/test_runner_bridge.py` (19): role mapping (no GitHub tools), registry restriction, honest unsupported-role failure, non-git worktree rejection, **candidate SHA binding with zero FI calls on stale candidate**, scoped edits + commit + `head_sha`, allowed/forbidden path enforcement, idempotent replay, verifier verdict (decision / candidate_sha / 64-hex diff_digest / evidence_kind), blocker-based rejection, cancellation → `cancelled`, runner job persisted for restart recovery, path globs, package roundtrip, review packet contents, manual review import (operator evidence + findings + event note), stale-candidate rejection, malformed verdict rejection.
  - `tests/test_integration_regressions.py` (13): strict approval identity matching (partial records, wrong branch/repo/PR, derived-operation bindings, cross-operation refusal), credential-free sandbox env, credential-free untrusted command execution, `..` traversal rejection, symlink escape rejection, non-destructive worktree recovery.
  - All pre-existing Mission-2 tests still pass.

## 7. What remains unverified

1. **Real FreeInference inference through the bridge** — the sandbox has no
   `FREEINFERENCE_API_KEY`; run `fi-pr smoke` and one real `fi-pr runner-task`
   on the user's machine.
2. **End-to-end Runner↔FI integration** — Runner is local; the adapter has never
   been dispatched by Runner. The command-adapter mechanism is verified from
   Runner's public source, but the wiring (§4.1/4.2) is untested.
3. **Local-branch Runner internals** — `secureexec` Bubblewrap flags,
   `review.go`, `verdict.go`, `qualification.go`, and the `VerdictFile` field are
   assumptions from the mission briefing; verify against the local checkout
   before implementing §4.3.
4. **Kontrol automatic forwarding** — the MCP rendezvous tools exist (verified in
   Kontrol source) but an MCP client session with worker/reviewer principals is
   required; until then the flow is operator-mediated.
5. **HTTP-worker mode of the bridge** — not implemented (the command adapter is
   the integration path); do not advertise `POST /runs` capabilities.
