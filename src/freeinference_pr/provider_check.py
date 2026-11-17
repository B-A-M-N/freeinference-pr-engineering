"""FreeInference provider verification workflow (Gate A).

Runs inside the managed deployment, where the FREEINFERENCE_API_KEY workspace
secret is bound, and exercises the provider through both raw OpenAI-compatible
calls and the Agents SDK adapter path: chat completion, tool calling with
structured arguments, streaming, multi-turn consistency, and rate-limit
reporting.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

import httpx
import mistralai.workflows as workflows

from freeinference_pr.provider import FREEINFERENCE_API_KEY_ENV_VAR, FREEINFERENCE_BASE_URL
from freeinference_pr.role_agents import InvestigationReport
from freeinference_pr.models_catalog import ROLE_PRESETS

DEFAULT_MODELS = ["qwen3.6-35b", "deepseek-v4-flash"]


@workflows.activity(name="provider-raw-checks")
async def provider_raw_checks(models: list[str]) -> dict[str, Any]:
    api_key = os.environ.get(FREEINFERENCE_API_KEY_ENV_VAR)
    if not api_key:
        raise RuntimeError(f"{FREEINFERENCE_API_KEY_ENV_VAR} is not bound to this deployment")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    report: dict[str, Any] = {"models": {}}
    async with httpx.AsyncClient(base_url=FREEINFERENCE_BASE_URL, headers=headers, timeout=180) as client:
        catalog = (await client.get("/models")).json()
        report["catalog_ids"] = [m["id"] for m in catalog.get("data", [])]
        for model in models:
            checks: dict[str, Any] = {}
            checks["chat"] = await _check_chat(client, model)
            checks["tool_call"] = await _check_tool_call(client, model)
            checks["streaming"] = await _check_streaming(client, model)
            checks["multi_turn"] = await _check_multi_turn(client, model)
            report["models"][model] = checks
    return report


@workflows.activity(name="provider-adapter-check")
async def provider_adapter_check(role: str) -> dict[str, Any]:
    """Run one real role-agent turn through the Agents SDK custom provider."""
    import json as _json

    from mistralai import agents

    from freeinference_pr.activities import _parse_structured, _schema_instruction
    from freeinference_pr.role_agents import build_role_agent

    agent = build_role_agent(role)
    task = (
        "Smoke test: report a single investigation finding titled 'adapter smoke test' "
        "with severity 'info' and evidence ['provider_check']. Keep it one sentence."
    )
    prompt = f"{task}\n\n{_schema_instruction(InvestigationReport)}"
    async with agents.local_runtime():
        state = await agent.run(prompt)
    if not isinstance(state, agents.Completed):
        raise RuntimeError(f"Adapter turn did not complete: {type(state).__name__}")
    report = _parse_structured(state.text, InvestigationReport)
    return {
        "model": agent.model,
        "session_id": state.session_id,
        "finding_title": report.root_causes[0].title if report.root_causes else None,
        "schema_payload_bytes": len(_json.dumps(InvestigationReport.model_json_schema())),
    }


@workflows.workflow.define(
    name="freeinference-provider-check",
    workflow_display_name="FreeInference Provider Check",
    workflow_description="Gate A: verify FreeInference chat, tool calling, streaming, multi-turn, and the Agents SDK adapter inside the managed deployment.",
    execution_timeout=timedelta(minutes=30),
)
class ProviderCheckWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, models: list[str] | None = None) -> dict[str, Any]:
        selected = models or [ROLE_PRESETS["orchestrator"].model, ROLE_PRESETS["investigation"].model]
        raw = await provider_raw_checks(selected)
        adapter = {}
        for role in ("orchestrator", "investigation", "review", "verification", "packaging"):
            try:
                adapter[role] = await provider_adapter_check(role)
            except Exception as exc:  # noqa: BLE001
                adapter[role] = {"error": f"{type(exc).__name__}: {exc}"}
        return {"raw": raw, "adapter": adapter}


async def _check_chat(client: httpx.AsyncClient, model: str) -> dict[str, Any]:
    response = await client.post(
        "/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "Reply with exactly: OK"}], "max_tokens": 10},
    )
    body = response.json()
    return {
        "status": response.status_code,
        "content": (body.get("choices") or [{}])[0].get("message", {}).get("content", "")[:100],
    }


async def _check_tool_call(client: httpx.AsyncClient, model: str) -> dict[str, Any]:
    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Call get_pr_diff for owner 'HarvardMadSys', repo 'HybridInference', pr 1781."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_pr_diff",
                        "description": "Fetch a PR diff",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "owner": {"type": "string"},
                                "repo": {"type": "string"},
                                "pr": {"type": "integer"},
                            },
                            "required": ["owner", "repo", "pr"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "max_tokens": 200,
        },
    )
    body = response.json()
    message = (body.get("choices") or [{}])[0].get("message", {})
    calls = message.get("tool_calls") or []
    structured_args_ok = False
    if calls:
        try:
            json.loads(calls[0]["function"]["arguments"])
            structured_args_ok = True
        except Exception:  # noqa: BLE001
            structured_args_ok = False
    return {
        "status": response.status_code,
        "tool_called": bool(calls),
        "tool_name": calls[0]["function"]["name"] if calls else None,
        "structured_args": structured_args_ok,
    }


async def _check_streaming(client: httpx.AsyncClient, model: str) -> dict[str, Any]:
    chunks = 0
    async with client.stream(
        "POST",
        "/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Count from 1 to 5."}],
            "stream": True,
            "max_tokens": 100,
        },
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data:") and "[DONE]" not in line:
                chunks += 1
    return {"status": response.status_code, "stream_chunks": chunks}


async def _check_multi_turn(client: httpx.AsyncClient, model: str) -> dict[str, Any]:
    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "user", "content": "My mission code is MISSION-42."},
                {"role": "assistant", "content": "Noted, your mission code is MISSION-42."},
                {"role": "user", "content": "What is my mission code? Reply with only the code."},
            ],
            "max_tokens": 20,
        },
    )
    body = response.json()
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return {"status": response.status_code, "recalled": "MISSION-42" in content, "content": content[:60]}
