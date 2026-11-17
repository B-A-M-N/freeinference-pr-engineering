# FreeInference PR Engineering

A unified, multi-agent PR engineering and review system that runs on **Mistral's
managed workflow infrastructure** and is reachable from **both** Mistral Vibe Web
(Vibe Work chat) and the **Vibe Code CLI** — the same missions, findings,
verification records, and approvals from either interface.

All PR-engineering model inference goes through **FreeInference**
(`https://freeinference.org/v1`, OpenAI-compatible). Mistral provides the
orchestration infrastructure and interfaces; no paid Mistral inference is used
inside the deployed PR engineering system.

## Architecture

```
Layer A  Interfaces          Vibe Web (chat workflow in Vibe Work)  |  Vibe CLI (Mistral MCP)
                                        \                /
Layer B  Managed orchestration   pr-engineering-assistant workflow (hardened, on-behalf-of)
                                 + 6 role agents (Agents SDK) + durable mission state
                                        |
Layer C  Inference            FreeInference (custom Agents SDK completion provider)
```

- **One mission = one long-running workflow execution.** Its durable state is the
  authoritative mission record, exposed via query handlers (`mission_state`,
  `findings`, `coverage`, `awaiting_approval`) and tagged with a stable
  `mission_key` search key. Vibe Web and the Vibe CLI address the *same*
  execution — no report copying between interfaces.
- **Deterministic pipeline**: `INTAKE → INVESTIGATION → [IMPLEMENTATION] →
  REVIEW → VERIFICATION → PR_PREPARATION → AWAITING_APPROVAL → COMPLETED/BLOCKED`,
  with bounded correction cycles (max 3), finding lifecycle
  `OPEN → ASSIGNED → CORRECTED → VERIFIED → CLOSED`, verification staleness on
  any source change, and explicit approval gates for every remote mutation.
- **Role agents** (all on FreeInference): orchestrator (`qwen3.6-35b`),
  investigation (`deepseek-v4-flash`), implementation (`deepseek-v4-flash`),
  independent review (`qwen3.6-35b`, deliberately a different model than the
  implementer), verification (`deepseek-v4-flash`), packaging (`qwen3.6-35b`).
  Model choices prioritize verified tool-calling + structured-output support
  over reputation.
- **Provider capacity**: max 2 concurrent FreeInference requests per worker
  (asyncio semaphore) + bounded retries; the provider's server-side queue is the
  cross-replica backstop.

## Repo layout

```
src/freeinference_pr/
  provider.py        FreeInference completion config (serializable, env-var key indirection)
  models_catalog.py  verified model catalog + per-role assignment
  mission_state.py   MissionState / Finding / VerificationRecord / lifecycle rules
  role_agents.py     the six role agents (Agents SDK, structured outputs)
  activities.py      GitHub evidence (connector), agent stages, local workspaces
  mission_store.py   cross-interface mission discovery + adoption
  pr_workflow.py     the conversational PR engineering workflow
  provider_check.py  Gate A provider verification workflow
  worker.py          managed worker entrypoint
cli/                 Vibe CLI provider profile + pr-missions skill
docs/                architecture, CLI setup, verification gates
tests/               offline tests (scripted model, lifecycle, registration)
```

## Deployment

The system deploys as a single managed workflow deployment built from this repo
(see `docs/DEPLOYMENT.md`). Required workspace secrets:

| Secret | Purpose |
| --- | --- |
| `FREEINFERENCE_API_KEY` | FreeInference API key (`hyi-...`), bound to the worker environment |

GitHub access uses the workspace **GitHub connector** with on-behalf-of
identity: user-initiated runs (Vibe Web chat, CLI-started) resolve the
triggering user's GitHub credential, so no PAT needs to be stored.

## Usage

**Vibe Web**: attach the *FreeInference PR Engineering* workflow in Vibe Work and
say e.g. `HarvardMadSys/HybridInference#1781: review the retry logic` — or give a
`mission_key` to continue an existing mission.

**Vibe CLI**: see `docs/CLI_SETUP.md` — the CLI uses the same Mistral MCP to
list missions, read findings/status, submit chat input to the same execution,
and record approvals.

## Development

```bash
uv sync
uv run pytest
```

Tests are offline: agent behavior is exercised with the Agents SDK's
`ScriptedModel`; provider behavior is verified inside the managed deployment by
the `freeinference-provider-check` workflow (Gate A).
