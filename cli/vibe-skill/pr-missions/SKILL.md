---
name: pr-missions
description: Operate the FreeInference PR engineering system from the Vibe CLI — list missions, read findings and verification records, continue missions, and submit approvals through Mistral MCP workflow tools. Use when the user asks to start, continue, inspect, or approve a PR engineering mission without copying reports between interfaces.
---

# PR missions via Mistral MCP

The PR engineering system runs as a managed Mistral workflow deployment. The
authoritative mission state lives in the running workflow execution, not in this
chat. Never copy mission reports between interfaces: query the execution.

## Discover missions

1. List executions of the mission workflow:

   `mistral_mcp_list_executions` with `workflow_identifier="pr-engineering-assistant"`
   and `statuses=["RUNNING"]`.

2. For each candidate execution, read its stable key and full state:

   - `mistral_mcp_query_workflow_execution` with `handler_name="mission_key"`
   - `mistral_mcp_query_workflow_execution` with `handler_name="mission_state"`
   - `mistral_mcp_query_workflow_execution` with `handler_name="findings"` (optional `input: {"status": "OPEN"}`)
   - `mistral_mcp_query_workflow_execution` with `handler_name="coverage"`
   - `mistral_mcp_query_workflow_execution` with `handler_name="awaiting_approval"`

   The `mission_key` for `owner/repo` PR `N` is `owner-repo-prN` (lowercase,
   non-alphanumerics collapsed to `-`), e.g. `harvardmadsys-hybridinference-pr1781`.

## Start a mission

`mistral_mcp_execute_workflow` with
`workflow_identifier="pr-engineering-assistant"` and input:

```json
{"repository": "owner/repo", "pr_number": 123, "objective": "...", "read_only": true}
```

or to continue an existing mission from a fresh chat:

```json
{"mission_key": "owner-repo-pr123", "instruction": "implement the identified corrections"}
```

## Interact with a running mission

The workflow pauses on chat inputs and approvals. To answer from the CLI:

1. `mistral_mcp_query_workflow_execution` with `handler_name="__get_pending_inputs"`
   — returns pending inputs with `task_id`, `label`, `input_schema`.
2. `mistral_mcp_update_workflow_execution` with `handler_name="__submit_input"` and
   `input: {"task_id": "<task_id>", "input": <object matching input_schema>}`.

For a pending remote-mutation approval, record the decision explicitly with
`handler_name="record_approval"` and
`input: {"operation": "...", "approved": true|false, "comment": "..."}`.

## Rules

- Read-only missions must never mutate remote GitHub state.
- Any push, PR create/update, comment, merge, or branch deletion requires the
  user's explicit approval naming operation, repository, branch, and commit.
- Never claim completion without verification records in `mission_state`.
- If an agent session cannot be resumed, reconstruct context from the mission
  record only; do not invent history.
- All PR engineering inference uses FreeInference models configured in
  `cli/freeinference-profile.toml`; do not switch providers.
