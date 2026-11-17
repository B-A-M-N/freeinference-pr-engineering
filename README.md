# FreeInference PR Engineering

A multi-agent PR engineering and review system with **two runtime paths**:

1. **Independent runtime (primary)** — a self-contained Python application that
   runs on any Linux machine or VM. Orchestration, state, and scheduling are
   owned by this codebase; model inference goes to **FreeInference**
   (`https://freeinference.org/v1`, OpenAI-compatible). No Mistral Cloud, no
   managed-deployment slots, no pay-as-you-go required. See
   [docs/INDEPENDENT_RUNTIME.md](docs/INDEPENDENT_RUNTIME.md).
2. **Optional Mistral managed path (preserved)** — the original conversational
   workflow (`pr-engineering-assistant`) for Vibe Web / Vibe CLI via Mistral
   managed deployments. It is fully implemented but requires a Mistral
   deployment slot (Free-plan workspaces have a quota of 0 — see
   [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). This path is optional; the
   independent runtime never needs it.

Both paths share the same engineering discipline: deterministic stage
transitions, bounded correction cycles, independent review, real test
execution, verification staleness, and explicit approval for every remote
GitHub mutation.

## The independent runtime

```
fi-pr submit octo/example#123            review a PR (read-only)
fi-pr submit octo/example#42 -o "fix the retry bug"
fi-pr submit "octo/example: add rate limiting to the client"
fi-pr status [job_id]                    jobs / tasks / findings / verifications / approvals
fi-pr approve appr_xxxxx                 authorize a pending publish operation
fi-pr resume job_xxxxx                   continue after approval or interruption
fi-pr log job_xxxxx [--limit N]          redacted event log
fi-pr artifacts job_xxxxx                drafts, commits, PR packages
fi-pr cancel job_xxxxx
fi-pr doctor                             runtime health checks
fi-pr smoke                              real FreeInference integration smoke test
```

## Runner-managed mode

The same codebase can run as a specialized worker subsystem inside the local
[Runner](https://github.com/B-A-M-N/runner) application: Runner owns task
lifecycle, scheduling, worktrees, approvals, and completion authority; FI
supplies the multi-agent engineering capability through the existing free
FreeInference access. Runner dispatches a TaskPackage; the bridge executes
exactly that bounded task (no scheduling, no GitHub tools, no publication) and
returns a SHA-bound, idempotent result.

```
fi-pr runner-task package.json        # execute one Runner task package
fi-pr review-packet job_xxxxx         # copyable external review packet
fi-pr review-import job_xxxxx v.json --candidate-sha SHA --reviewer R --transport T
```

Role mapping: Runner `planner` → repository analyst, `implementer` →
implementer, `verifier` → verifier/security review. Edits are confined to the
package's `allowedPaths`/`forbiddenPaths`; worktree HEAD must match the
package's `baseSha` before any inference call; results replay idempotently per
`idempotencyKey`; `maxMinutes` is enforced. External reviews (ChatGPT via the
Kontrol rendezvous, Gemini, Mistral user-facing sessions) are operator-mediated
and recorded as operator-provided evidence — never as publish authority. See
[docs/RUNNER_INTEGRATION.md](docs/RUNNER_INTEGRATION.md) for the verified-vs-
assumed contract split and exact Runner-side integration steps.

### How a job runs

```
INTAKE → ANALYSIS → IMPLEMENTATION → TESTING → REVIEW → VERIFICATION
       → (CORRECTION, bounded) → PACKAGING → AWAITING_APPROVAL
       → PUBLISHED | COMPLETED | BLOCKED | FAILED
```

- **Eight roles**, minimum necessary selected by a deterministic planner:
  orchestrator, repository analyst, implementer, code reviewer, security
  reviewer, verification agent, integration agent, PR manager. Different
  FreeInference models per role (configurable).
- **Parallel execution** of independent agents (reviewers run concurrently;
  multiple implementation units run in per-unit git worktrees and are
  integrated via cherry-pick), bounded by a configurable FI concurrency
  semaphore (default 2).
- **Isolation**: every job works in its own clone/worktree under
  `FI_WORKSPACE_ROOT`; agents never touch the user's checkout.
- **Least privilege**: agents never hold GitHub write tools. Publishing tools
  are restricted to the `pr_manager` role and refuse to act without a recorded,
  user-approved authorization for the exact operation (repository, branch,
  commit). One approval covers only its own mechanical sub-steps.
- **Untrusted content**: repository files, issues, PR text, and tool output are
  fenced as data; secret patterns are redacted from every tool result, event,
  and log line.
- **Persistence & recovery**: SQLite (WAL) stores jobs, tasks, findings,
  verifications, approvals, artifacts, and an append-only event log. On
  restart, `recover()` marks stale-hearted tasks `interrupted`; completed work
  is never re-run and jobs can be resumed with `fi-pr resume`.

## Installation

```bash
git clone https://github.com/B-A-M-N/freeinference-pr-engineering
cd freeinference-pr-engineering
./scripts/bootstrap.sh          # creates .venv, installs deps, runs tests
source .venv/bin/activate
cp .env.example .env            # then fill in FREEINFERENCE_API_KEY
fi-pr doctor                    # health checks
fi-pr smoke                     # real FI integration check (needs the key)
```

Python 3.11+ and `git` are required. Docker is optional (a Dockerfile for the
preserved managed worker exists; the independent runtime needs none).

### Configuration

All configuration is environment-driven (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `FREEINFERENCE_API_KEY` | FreeInference API key (`hyi-...`) — required |
| `FREEINFERENCE_BASE_URL` | default `https://freeinference.org/v1` |
| `FI_ORCHESTRATOR_MODEL` / `FI_IMPLEMENTER_MODEL` / `FI_REVIEWER_MODEL` / `FI_ANALYST_MODEL` / `FI_SECURITY_MODEL` / `FI_VERIFIER_MODEL` / `FI_INTEGRATION_MODEL` / `FI_PACKAGER_MODEL` | per-role model IDs |
| `FI_MAX_CONCURRENT` | max simultaneous FI requests (default 2) |
| `FI_STATE_PATH` | SQLite state file (default `state/freeinference-pr.db`) |
| `FI_WORKSPACE_ROOT` | isolated worktree root (default `workspaces`) |
| `FI_MAX_CORRECTION_CYCLES` | bounded correction loop (default 3) |
| `FI_TASK_TIMEOUT` / `FI_HEARTBEAT_STALE` | stall detection / recovery |
| `GITHUB_TOKEN` | least-privilege GitHub token (read for reviews; repo contents+PR write only for publishing) |

No Mistral API key is used anywhere in the independent runtime.

## Repo layout

```
src/freeinference_pr/
  config.py          FIConfig: env-driven runtime configuration
  fi_provider.py     FreeInference client: chat, streaming, tools, retries, classification
  runtime.py         native agent runtime (bounded tool-calling loop per role)
  agent_models.py    structured outputs (analysis, edits, reviews, verification, package)
  tools.py           role-scoped tools: git, workspace, GitHub REST, approval-gated writes
  store.py           SQLite persistence: jobs/tasks/findings/verifications/approvals/events
  approvals.py       approval gate (single enforcement point for privileged operations)
  engine.py          deterministic stage machine + scheduling + worktrees + publishing
  cli.py             fi-pr command-line interface
  runner_bridge.py   Runner-managed mode: task-package adapter, review packets, manual review import
  smoke.py           real-FI integration smoke test
  redaction.py       secret redaction for traces, logs, tool results
  provider.py, role_agents.py, pr_workflow.py, ...   preserved optional Mistral managed path
docs/
  INDEPENDENT_RUNTIME.md   architecture, guarantees, limitations
  RUNNER_INTEGRATION.md    Runner worker-subsystem integration (verified vs. assumed contract)
integration/runner/  Go contract harness against Runner's real CommandAdapter
scripts/runner_qualification_live.sh  live-FI qualification (user's machine, disposable repo)
  INVENTORY.md             preserved-work inventory (commit 0fb6298 baseline)
  DEPLOYMENT.md            optional Mistral managed path
tests/               119 offline tests (mocked FI, fake GitHub, local git fixtures)
```

## Development

```bash
uv sync
uv run pytest          # 119 offline tests; no network, no keys needed
```

Tests are offline by design: FreeInference is mocked at the HTTP boundary,
GitHub REST is faked in-memory, and git operations run against local fixture
repositories. A passing test suite therefore does **not** constitute verified
real-provider integration — run `fi-pr smoke` with real credentials for that,
and do not treat mocked success as a claim of live verification.
