# The independent FreeInference runtime

This document describes the primary runtime: a self-contained, multi-agent PR
engineering system that runs on a local Linux machine or VM with no Mistral
Cloud dependency.

## Layer responsibilities

| Layer | Owner | Notes |
| --- | --- | --- |
| Inference | FreeInference API | OpenAI-compatible `/v1`; key via `FREEINFERENCE_API_KEY` |
| Orchestration | `engine.py` | Deterministic stage machine; model never decides scheduling |
| Execution | `runtime.py` + `tools.py` | Bounded tool-calling loop per role; role-scoped tools |
| State | `store.py` | SQLite (WAL): jobs, tasks, findings, verifications, approvals, events, artifacts, idempotency |
| Verification | verifier/integrator agents + engine | Real commands, real exit codes; independent review of the actual diff |
| Persistence | `store.py` | Restart-safe; `recover()` classifies interrupted work |

## Job lifecycle

```
INTAKE → ANALYSIS → IMPLEMENTATION → TESTING → REVIEW → VERIFICATION
      → (CORRECTION ⇢ bounded by FI_MAX_CORRECTION_CYCLES) → PACKAGING
      → AWAITING_APPROVAL → PUBLISHED | COMPLETED
any stage → BLOCKED (verification failed / uncorrectable blockers)
any stage → FAILED (engine error) | CANCELLED (user) | INTERRUPTED (restart)
```

- `review_pr` jobs (from a PR URL or `owner/repo#123`) are read-only: fetch PR
  evidence → analyst → parallel code + security review → CI interpretation →
  review report. Publishing a comment still requires approval.
- `request` jobs (issue or natural language) run the full implement pipeline.
  Blocking findings (severity `critical`/`high`) trigger bounded correction
  cycles; if verification fails or the cycle bound is hit, the job is
  **blocked** — never reported as complete.
- Agent-reported success is never accepted as proof: verification records
  actual exit codes, review findings are inspected against the real `git diff`,
  and verifications are invalidated whenever new edits land.

## Coordination

- Parallel independent tasks: code + security reviewers run concurrently;
  multiple implementation units run in per-unit worktrees concurrently and are
  integrated into the primary worktree via cherry-pick (patch-apply fallback).
  Integration failure becomes a high-severity finding and blocks the job.
- All FI traffic passes through one configurable semaphore (default 2) with
  bounded exponential backoff, `Retry-After` support, timeouts, and
  cancellation.
- Task heartbeats + `FI_TASK_TIMEOUT` detect stalls; `store.recover()` marks
  stale running tasks `interrupted` after a restart, and `fi-pr resume`
  continues the job. Completed tasks are never re-run; worktree creation is
  idempotent via the store's kv table.

## Security model

- **Write separation**: only `pr_manager` holds GitHub write tools, and each
  write handler consults the approval gate. The gate passes only when the store
  holds an `approved` record for the operation (or its declared parent
  authorization, e.g. `publish_branch_and_pr` covering `create_branch`,
  `commit_files`, `create_pull_request` for the same repository/branch).
- **No auto-merge**, no force-push (`force=False` on ref updates), no branch
  protection bypass; existing open PRs are updated rather than duplicated.
- **Prompt injection**: repository content, issues, PR text, and tool output
  are wrapped in `<<<UNTRUSTED-CONTENT-BEGIN>>>` fences and the runtime system
  prompt treats them as data only.
- **Secret redaction**: `redaction.py` patterns (`hyi-`, `ghp_`, `github_pat_`,
  `gho_`, `sk-`, Bearer, `key=value`) applied to every tool result, event
  payload, artifact, and CLI log line.
- **Workspace isolation**: agents' file tools resolve paths through an escape
  guard; shells run with a restricted environment and `HOME` set to the
  worktree.
- **Least-privilege GitHub token**: reads need none to minimal scope; the
  publish path needs `contents:write` + `pull-requests:write` on the target
  repository only.

## Known limitations (accurate as of this revision)

1. **Single-user, single-process.** SQLite and one asyncio loop; no
   multi-worker lease protocol. Sufficient for the stated deployment.
2. **Real-provider integration is not yet verified.** All tests are mocked;
   `fi-pr smoke` and `fi-pr doctor` must be run with real credentials to verify
   the live path. Do not treat the passing offline suite as live verification.
3. **Publishing is exercised only against a fake GitHub.** The Git data API
   write path (branch → blobs → tree → commit → ref) is implemented and
   unit-tested logically, but a real end-to-end PR publication has not been
   performed from this runtime.
4. **CI interpretation is model-derived** for `review_pr` jobs (clearly labeled
   in verification records); local verification uses real command results.
5. **No web dashboard.** The CLI is the operational interface, per the
   prioritization in the requirements.
6. **The optional Mistral managed path remains blocked** by the workspace's
   managed-deployment quota (0 on Free plan without pay-as-you-go); it is
   preserved code, not a working deployment.
