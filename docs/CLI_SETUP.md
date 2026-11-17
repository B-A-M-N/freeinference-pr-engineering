# Vibe CLI setup

## 1. FreeInference model provider

Copy the `[[providers]]` block and model presets from
`cli/freeinference-profile.toml` into your Vibe CLI provider configuration.
Keep your existing profiles; only add the `freeinference` provider and the
`pr-engineering` profile. Export the key in your shell:

```bash
export FREEINFERENCE_API_KEY="hyi-..."   # your key; never commit it
```

## 2. Mistral MCP

The Vibe CLI talks to the managed workflow through Mistral's MCP server
(`https://api.mistral.ai/mcp`) using your Mistral workspace login. Verify with:

1. `mistral_mcp_whoami` — must show the jlondonlabs workspace.
2. `mistral_mcp_list_workflow_deployments` — `freeinference-pr-engineering` is serving.
3. `mistral_mcp_list_workflows` — `pr-engineering-assistant` and
   `freeinference-provider-check` are registered.

## 3. PR missions skill

Install the `cli/vibe-skill/pr-missions` skill into your Vibe CLI skills
directory. It teaches the CLI to:

- list missions: `list_executions(workflow_identifier="pr-engineering-assistant", statuses=["RUNNING"])`
- read state: query handlers `mission_state`, `findings`, `coverage`, `awaiting_approval`
- start/continue missions: `execute_workflow` with `mission_key` or fresh input
- answer chat inputs: `__get_pending_inputs` → `__submit_input`
- record approvals: `record_approval`

## 4. Local repository work (optional)

When the CLI runs inside a local checkout, implementation may use the local
tree under these rules: verify repository identity first, work on an isolated
branch, preserve existing changes, never push without approval, and report the
commit SHA back to the mission (via `execute_workflow` with `mission_key` and an
`instruction`, or `record_approval`/`__submit_input`). The cloud workflow stays
the authority for mission state, findings, and completion; local work without a
checkout delegates to the managed execution environment.
