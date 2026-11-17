# Verification gates

Recorded results live here as gates are run against the deployed system.

## Catalog verification (2026-11-16, sandbox)

`GET https://freeinference.org/v1/models` → 3 models:

| model | context | max output | features |
| --- | --- | --- | --- |
| `qwen3.6-35b` | 262,144 | 8,192 | tools, json_mode, structured_outputs |
| `deepseek-v4-flash` | 1,000,000 | 393,216 | tools, json_mode, structured_outputs |
| `bge-m3` | 8,192 | — | embeddings (not used for inference) |

Chat completions require `Authorization: Bearer hyi-...` (401 without key).

## Gate A — Provider (managed deployment)

Status: **pending secret binding** — requires the `FREEINFERENCE_API_KEY`
workspace secret to exist. Run the `freeinference-provider-check` workflow after
deployment; it verifies chat, tool calling with structured arguments, streaming,
multi-turn recall, and the Agents SDK adapter for all six roles.

## Gate B/C — Managed agents + workflow

Status: pending deployment.

## Gate D — GitHub

Status: pending deployment (connector evidence fetches inside a mission run).

## Gate E/F/G/H

Status: pending deployment.

## Known limitations

- Provider concurrency: the semaphore caps FreeInference requests per worker
  process; across worker replicas the provider's server-side queue is the
  backstop (its API exposes `queue_size`).
- Implementation-stage workspaces are worker-local (`/tmp/...`), re-cloned per
  prepare; commits are recorded in the mission record. No remote push happens
  without an explicit approval record.
- The `github_app` connector credential is user-scoped: unattended schedules
  cannot use it (OBO only). This system is intentionally user-initiated.
