# Deployment

## Prerequisites

1. **Workspace secret** `FREEINFERENCE_API_KEY` — create it in Mistral Studio
   (Secrets), value is your FreeInference key (`hyi-...`). Never paste it in
   chat or code.
2. **GitHub connector** — the workspace GitHub connector with a valid user
   credential (already configured in this workspace as `github_app`).
3. The repo must be readable by the platform's GitHub App connector.

## Steps

```bash
git push origin main   # deploy builds from a pushed commit
```

Then create/redeploy the managed deployment from the pushed commit SHA with the
Mistral MCP tool `deploy_workflow_deployment`:

- repository: `https://github.com/B-A-M-N/freeinference-pr-engineering`
- revision: the pushed commit SHA
- deployment name: `freeinference-pr-engineering`
- build directory: `.` (repo root), dockerfile path: `Dockerfile`
- `hardened: true` (required: the workflow runs on-behalf-of the triggering user)
- secrets: `[{"env_var_name": "FREEINFERENCE_API_KEY", "reference": "secret:workspace:FREEINFERENCE_API_KEY"}]`

Wait for readiness (`wait_workflow_deployment_ready` / `get_workflow_deployment`
with `ready=true` and `deployed_revision` == pushed SHA).

## Verify

- **Gate A**: execute the `freeinference-provider-check` workflow on the
  deployment; every model's chat/tool-call/streaming/multi-turn checks and all
  six adapter smoke tests must pass.
- **Gates B–C**: execute `pr-engineering-assistant` with a real PR in read-only
  mode; confirm stage transitions and query handlers.
- **Gate D**: the GitHub connector evidence fetches (overview, files, diff, CI
  status) must return live data inside a mission run.
- **Gate E**: attach the workflow in Vibe Work and run a mission from chat.
- **Gate F**: from the Vibe CLI, discover and query the mission via Mistral MCP.
- **Gate G**: start in Web → continue in CLI → return to Web; same mission_key,
  same findings.
- **Gate H**: full read-only review of a real PR; no remote mutations.

See `docs/GATES.md` for recorded results.
