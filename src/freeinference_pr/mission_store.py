"""Cross-interface mission discovery and adoption.

Vibe Web and Vibe CLI do not share chat history. The authoritative cross-interface
state is the *mission execution itself*: every mission is one long-running
``pr-engineering-assistant`` execution whose durable state is exposed through
query handlers and tagged with a stable ``mission_key`` search key. A new
execution (a new web chat, or a CLI-initiated continuation) adopts an existing
mission by loading its snapshot through the platform API and becoming the new
authoritative execution for that mission.
"""

from __future__ import annotations

from typing import Any

import mistralai.workflows as workflows
from mistralai.workflows.client import get_mistral_client

MISSION_WORKFLOW_NAME = "pr-engineering-assistant"


@workflows.activity(name="find-mission-executions")
async def find_mission_executions(mission_key: str) -> list[dict[str, Any]]:
    client = get_mistral_client()
    response = await client.workflows.runs.list_runs_async(
        workflow_identifier=MISSION_WORKFLOW_NAME,
        status="RUNNING",
        order="desc",
        page_size=50,
        include_internal=False,
    )
    matches: list[dict[str, Any]] = []
    runs = response.runs if response and getattr(response, "runs", None) else []
    for run in runs:
        execution_id = getattr(run, "execution_id", None) or getattr(run, "workflow_execution_id", None)
        if not execution_id:
            continue
        try:
            result = await client.workflows.executions.query_workflow_execution_async(
                workflow_execution_id=execution_id, handler_name="mission_key"
            )
        except Exception:  # noqa: BLE001 - skip executions without the handler
            continue
        if _query_result_value(result) == mission_key:
            matches.append({"execution_id": execution_id, "started_at": str(getattr(run, "start_time", ""))})
    return matches


def _query_result_value(result: Any) -> Any:
    for attr in ("result", "value", "output"):
        if hasattr(result, attr):
            return getattr(result, attr)
    return result


@workflows.activity(name="load-mission-snapshot")
async def load_mission_snapshot(mission_key: str) -> dict[str, Any] | None:
    """Load the newest RUNNING execution's mission state, or None if not found."""
    client = get_mistral_client()
    executions = await find_mission_executions(mission_key)
    if not executions:
        return None
    newest = executions[0]
    result = await client.workflows.executions.query_workflow_execution_async(
        workflow_execution_id=newest["execution_id"], handler_name="mission_state"
    )
    snapshot = _query_result_value(result)
    if isinstance(snapshot, str):
        import json

        snapshot = json.loads(snapshot)
    if not snapshot:
        return None
    snapshot["previous_execution_id"] = newest["execution_id"]
    return snapshot


@workflows.activity(name="signal-mission-adopted")
async def signal_mission_adopted(execution_id: str, new_execution_id: str) -> None:
    """Tell the previous mission execution it was adopted, so it stops waiting."""
    client = get_mistral_client()
    await client.workflows.executions.signal_workflow_execution_async(
        workflow_execution_id=execution_id,
        signal_name="mission_adopted",
        input={"new_execution_id": new_execution_id},
    )
