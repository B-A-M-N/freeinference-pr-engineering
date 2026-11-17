# Independent runtime inventory

Baseline inspected: commit `0fb6298ce0e7464bbbaa00f0048dd9fc8e9d9c7a`
("Add pr-missions CLI skill"). This inventory records what was preserved, what
depends on Mistral Cloud, and what the independent runtime added. Nothing
working was discarded; no second disconnected implementation was created — the
independent runtime reuses the same package (`freeinference_pr`) and the same
engineering rules.

## Already worked at 0fb6298 (preserved as-is)

| Component | File(s) | Status |
| --- | --- | --- |
| FreeInference completion provider for the Agents SDK | `provider.py`, `models_catalog.py` | Works; verified model catalog from live `/v1/models`; preserved for the optional Mistral path |
| Mission state model (findings, lifecycle, verification staleness) | `mission_state.py` | Works; lifecycle rules mirrored by the independent store |
| Six role agents (Agents SDK definitions) | `role_agents.py` | Works under the Agents SDK; preserved for the optional Mistral path |
| Deterministic managed workflow | `pr_workflow.py`, `activities.py` | Works but requires Mistral managed deployment |
| Cross-interface mission discovery | `mission_store.py` | Works but requires Mistral deployment records |
| Gate A provider-check workflow | `provider_check.py` | Works but requires Mistral managed deployment |
| Managed worker entrypoint | `worker.py` | Works but requires Mistral managed deployment |
| Vibe CLI profile + skill | `cli/` | Works against a deployed workflow |
| Offline tests (21) | `tests/test_*.py` (pre-existing) | All still pass unchanged |

## Mistral-Cloud-dependent (isolated, optional)

`provider.py`, `role_agents.py`, `pr_workflow.py`, `provider_check.py`,
`worker.py`, `mission_store.py`, `activities.py` depend on
`mistralai-workflows` / `mistralai-agents` and a managed deployment. They are
kept untouched as the optional Vibe Web path (`docs/DEPLOYMENT.md`). They are
**not** imported by the independent runtime's engine, CLI, or tests.

## Agents SDK investigation result

The Agents SDK local runtime (`_AgentsSdkLocalRuntime`) can run without Mistral
Cloud — the completion provider is app-owned and sessions are in-memory or a
JSON journal — but the API is deprecated and it does not support structured
outputs with a custom completion provider (raises
`UnsupportedAgentFeatureError`), which every role needs. Decision: the
independent runtime uses a **native thin runtime** (`runtime.py`) instead;
the SDK path stays only for the optional managed deployment.

## Missing before the pivot → added by the independent runtime

| Missing capability | Now provided by |
| --- | --- |
| Direct FI client (no Mistral SDK in the inference path) | `fi_provider.py` — chat, streaming, tool calls, retries with bounded backoff + `Retry-After`, timeouts, cancellation, concurrency semaphore (default 2), usage reporting, error classification, capability detection |
| Application-owned orchestration | `engine.py` — deterministic stage machine, minimum-role planning, dependency-aware scheduling, parallel independent agents, bounded correction cycles |
| Persistence & recovery | `store.py` — SQLite WAL: jobs, tasks, findings, verifications, approvals, artifacts, append-only events, idempotency kv, `recover()` |
| Native agent execution | `runtime.py` — bounded tool-calling loop, structured-output parsing, per-role instructions, turn limits |
| Role-scoped tools & least privilege | `tools.py` — git/workspace tools, GitHub REST reads, write tools restricted to `pr_manager` + approval-gated; path-escape guards; untrusted-content fencing |
| Approval enforcement | `approvals.py`, `store.py` approvals table — publish only with a recorded, matching authorization |
| Operational interface | `cli.py` (`fi-pr`), `__main__.py` |
| Real verification execution | verifier/integrator `run_commands` tool + engine `_execute_commands` (real exit codes recorded) |
| Security hygiene | `redaction.py` (secret redaction everywhere), untrusted-content wrapping, restricted env passthrough |
| Health checks & install path | `cli.py doctor`, `smoke.py`, `scripts/bootstrap.sh`, `.env.example` |

## Verification coverage added

`tests/test_fi_provider.py` (auth, discovery, chat parsing, retries,
streaming, concurrency cap, protocol errors),
`tests/test_runtime.py` (tool roundtrip, tool-error feedback, untrusted
fencing, turn limits, structured parsing),
`tests/test_store.py` (restart persistence, recovery, dedupe, approvals),
`tests/test_tools.py` (escape guards, role denial, redaction, real local git),
`tests/test_engine.py` (end-to-end implement job, approval enforcement +
publish, correction-cycle bound, verification failure, review job with parallel
reviewers, restart survival, worktree isolation),
`tests/test_cli.py` (target parsing, commands, doctor/submit guards).

**Not covered by tests (and not claimed):** live FreeInference calls (`fi-pr
smoke` covers this with real credentials) and real GitHub mutations (the
publish path is exercised against an in-memory fake only).
