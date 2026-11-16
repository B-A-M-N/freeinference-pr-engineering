"""Deterministic activities: GitHub evidence, FreeInference agent stages, local workspaces.

All remote I/O and all side effects live here; the workflow stays deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai import agents
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import ToolCallClient, connector

from freeinference_pr.mission_state import MissionState
from freeinference_pr.role_agents import OUTPUT_TYPES, build_role_agent

# The GitHub connector slot. Under an on_behalf_of workflow this resolves the
# triggering user's GitHub credential (run_as="auto"), which is why the
# deployment must be hardened.
github_connector = connector("github_app")

# Provider capacity: at most two simultaneous FreeInference requests per worker
# process. The provider's own server-side queue (its error payloads carry
# queue_size) is the backstop across worker replicas; bounded retries absorb 429s.
_PROVIDER_SEMAPHORE: asyncio.Semaphore | None = None

MAX_CONCURRENT_PROVIDER_REQUESTS = 2
MISSION_WORKSPACE_ROOT = Path(os.environ.get("MISSION_WORKSPACE_ROOT", "/tmp/freeinference-pr-missions"))


def _provider_semaphore() -> asyncio.Semaphore:
    global _PROVIDER_SEMAPHORE
    if _PROVIDER_SEMAPHORE is None:
        _PROVIDER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_PROVIDER_REQUESTS)
    return _PROVIDER_SEMAPHORE


def _unwrap_mcp_result(raw: Any) -> Any:
    """Connector results are MCP-wrapped (content[0].text, JSON-parse); parse defensively."""
    if isinstance(raw, dict):
        content = raw.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                text = first["text"]
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return raw
    return raw


@workflows.activity(name="gh-call")
async def gh_call(
    tool_name: str,
    arguments: dict[str, Any],
    github: ToolCallClient = Depends(github_connector),
) -> Any:
    """Call one GitHub connector tool as the triggering user (read-only tools only)."""
    result = await github.call_tool(tool_name=tool_name, arguments=arguments)
    return _unwrap_mcp_result(result)


async def fetch_pr_evidence(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    """Read-only PR evidence bundle: overview, files, diff, CI status, review comments."""
    evidence: dict[str, Any] = {"owner": owner, "repo": repo, "pr_number": pr_number}
    for key, tool, args in [
        ("overview", "pull_request_read", {"method": "get", "owner": owner, "repo": repo, "pullNumber": pr_number}),
        ("files", "pull_request_read", {"method": "get_files", "owner": owner, "repo": repo, "pullNumber": pr_number}),
        ("diff", "pull_request_read", {"method": "get_diff", "owner": owner, "repo": repo, "pullNumber": pr_number}),
        ("ci_status", "pull_request_read", {"method": "get_status", "owner": owner, "repo": repo, "pullNumber": pr_number}),
        (
            "review_comments",
            "pull_request_read",
            {"method": "get_review_comments", "owner": owner, "repo": repo, "pullNumber": pr_number},
        ),
    ]:
        try:
            evidence[key] = await gh_call(tool, args)
        except Exception as exc:  # noqa: BLE001 - evidence is best-effort, failures are recorded
            evidence[key] = {"error": f"{type(exc).__name__}: {exc}"}
    return evidence


async def fetch_file(owner: str, repo: str, path: str, ref: str | None = None) -> Any:
    args: dict[str, Any] = {"owner": owner, "repo": repo, "path": path}
    if ref:
        args["ref"] = ref
    return await gh_call("get_file_contents", args)


def _schema_instruction(model_cls: type) -> str:
    import json

    schema = json.dumps(model_cls.model_json_schema())
    return (
        "Reply with a single JSON object and nothing else (no prose, no code fences), "
        f"matching exactly this JSON Schema:\n{schema}"
    )


def _parse_structured(text: str, model_cls: type[BaseModel]) -> Any:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("reply contains no JSON object")
    return model_cls.model_validate_json(text[start : end + 1])


@workflows.activity(name="run-role-stage")
async def run_role_stage(role: str, task: str, mission_json: str, evidence_json: str) -> dict[str, Any]:
    """Run one bounded role-agent task on FreeInference and return its structured report.

    Context is passed in explicitly; nothing is carried in process memory between
    stages, so replay and worker restarts are safe.

    Structured output: the installed Agents SDK (1.1.3) raises
    UnsupportedAgentFeatureError for output_type= with a custom completion
    provider, so the schema is requested in the prompt and validated here, with
    one corrective retry.
    """
    output_type = OUTPUT_TYPES.get(role)
    agent = build_role_agent(role)
    prompt = f"# Mission record\n{mission_json}\n\n# Evidence bundle\n{evidence_json}\n\n# Task\n{task}"
    if output_type is not None:
        prompt = f"{prompt}\n\n# Output format\n{_schema_instruction(output_type)}"
    async with _provider_semaphore():
        async with agents.local_runtime():
            state = await agent.run(prompt)
    if not isinstance(state, agents.Completed):
        raise RuntimeError(f"Role {role} turn did not complete: {type(state).__name__}: {getattr(state, 'text', '')[:500]}")
    if output_type is None:
        return {"report": {"text": state.text}, "session_id": state.session_id, "turn_id": state.turn_id}
    try:
        report = _parse_structured(state.text, output_type)
    except Exception as first_error:  # noqa: BLE001 - one corrective retry
        async with _provider_semaphore():
            async with agents.local_runtime():
                retry_state = await agent.run(
                    f"{prompt}\n\nYour previous reply was invalid: {first_error}. "
                    "Answer again with a single JSON object strictly matching the schema."
                )
        if not isinstance(retry_state, agents.Completed):
            raise RuntimeError(f"Role {role} retry did not complete: {type(retry_state).__name__}")
        report = _parse_structured(retry_state.text, output_type)
        state = retry_state
    return {"report": report.model_dump(), "session_id": state.session_id, "turn_id": state.turn_id}


@workflows.activity(name="run-orchestrator")
async def run_orchestrator(task: str, mission_json: str) -> dict[str, Any]:
    agent = build_role_agent("orchestrator")
    async with _provider_semaphore():
        async with agents.local_runtime():
            state = await agent.run(f"# Mission record\n{mission_json}\n\n# Task\n{task}")
    if not isinstance(state, agents.Completed):
        raise RuntimeError(f"Orchestrator turn did not complete: {type(state).__name__}")
    return {"text": state.text, "session_id": state.session_id}


def _repo_dir(mission_id: str) -> Path:
    return MISSION_WORKSPACE_ROOT / mission_id / "repo"


@workflows.activity(name="prepare-workspace")
async def prepare_workspace(mission_id: str, repository: str, base_ref: str, branch: str) -> dict[str, Any]:
    """Clone the repository read-only into an isolated worker-local worktree.

    The workspace is per-mission and disposable: every prepare re-clones from the
    remote, so retries and worker restarts never reuse stale trees. No push, no
    force, no upstream mutation happens here.
    """
    repo_dir = _repo_dir(mission_id)
    if repo_dir.exists():
        shutil.rmtree(repo_dir.parent)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repository}.git"
    _run(["git", "clone", "--no-checkout", url, str(repo_dir)])
    _run(["git", "-C", str(repo_dir), "checkout", base_ref or "HEAD"])
    _run(["git", "-C", str(repo_dir), "switch", "-c", branch])
    return {"workspace": str(repo_dir), "branch": branch}


@workflows.activity(name="apply-edits")
async def apply_edits(
    mission_id: str, edits: list[dict[str, Any]], commit_message: str
) -> dict[str, Any]:
    """Apply structured file edits in the mission workspace and commit locally."""
    repo_dir = _repo_dir(mission_id)
    if not repo_dir.exists():
        raise RuntimeError(f"Mission workspace missing: {repo_dir}. Run prepare-workspace first.")
    for edit in edits:
        target = (repo_dir / edit["path"]).resolve()
        if not str(target).startswith(str(repo_dir.resolve())):
            raise RuntimeError(f"Edit path escapes workspace: {edit['path']}")
        if edit.get("action") == "delete":
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit["content"])
    _run(["git", "-C", str(repo_dir), "add", "-A"])
    dirty = _run(["git", "-C", str(repo_dir), "status", "--porcelain"]).stdout.strip()
    if not dirty:
        return {"committed": False, "commit_sha": None, "note": "no effective changes"}
    _run(["git", "-C", str(repo_dir), "commit", "-m", commit_message])
    sha = _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip()
    return {"committed": True, "commit_sha": sha}


@workflows.activity(name="run-tests")
async def run_tests(mission_id: str, commands: list[str]) -> dict[str, Any]:
    """Execute the given commands in the mission workspace; record real exit codes."""
    repo_dir = _repo_dir(mission_id)
    if not repo_dir.exists():
        raise RuntimeError(f"Mission workspace missing: {repo_dir}")
    results = []
    for command in commands:
        proc = subprocess.run(
            command, shell=True, cwd=repo_dir, capture_output=True, text=True, timeout=1800
        )
        results.append(
            {
                "command": command,
                "exit_code": proc.returncode,
                "stdout_tail": proc.stdout[-4000:],
                "stderr_tail": proc.stderr[-4000:],
            }
        )
    return {"results": results}


@workflows.activity(name="workspace-diff")
async def workspace_diff(mission_id: str) -> str:
    repo_dir = _repo_dir(mission_id)
    return _run(["git", "-C", str(repo_dir), "diff", "HEAD"]).stdout


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr[-2000:]}")
    return proc


def mission_state_to_json(state: MissionState) -> str:
    return state.model_dump_json(exclude_none=True)
