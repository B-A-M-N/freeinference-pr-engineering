"""Runner-managed execution mode: FI agents as a Runner worker subsystem.

Runner (local Go application) owns task lifecycle, scheduling, worktrees,
approvals, and completion authority. This module makes FreeInference PR
Engineering usable as a specialized worker under Runner's adapter contract
(``internal/workers/workers.go``): Runner dispatches a TaskPackage (written to
disk and passed via ``RUNNER_TASK_PACKAGE``), this bridge executes exactly that
bounded task with FI-backed agents, and Runner collects the result.

In this mode the bridge deliberately has LESS authority than the independent
runtime:

- No scheduling, no correction-cycle ownership, no PR publication, no GitHub
  write tools, no approval-gated publishing. Runner owns all of that.
- Execution is confined to the worktree Runner provides (``pkg.worktree``);
  the bridge never clones or creates workspaces of its own.
- File edits are restricted to ``allowedPaths`` and excluded by
  ``forbiddenPaths``.
- The candidate SHA is bound: the worktree HEAD must equal ``baseSha`` before
  work starts, and the resulting HEAD is reported for Runner's SHA-pinned
  verification.
- Execution is idempotent per ``idempotencyKey``: replaying a package returns
  the recorded result instead of re-running (retry/restart safety).
- Time budget (``maxMinutes``) is enforced; expiry is reported honestly as a
  failed/cancelled run, never as success.

Role mapping (Runner responsibility -> FI specialist):
    planner     -> repository_analyst (bounded analysis and planning)
    implementer -> implementer (bounded edits + regression tests)
    verifier    -> verifier + security review capabilities (correctness and
                   security review evidence; Runner's verifier remains the gate)

External review handoff (no paid APIs): ``build_review_packet`` renders a
complete, copyable review packet for ChatGPT (via the existing Kontrol review
rendezvous), Gemini, or Mistral user-facing sessions; ``import_manual_review``
records the operator-submitted verdict as OPERATOR-PROVIDED evidence — it is
never treated as an authenticated automated model response and never grants
publish/merge authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from freeinference_pr.agent_models import AnalysisReport, EditsReport, ReviewReport
from freeinference_pr.config import FIConfig
from freeinference_pr.redaction import redact_obj
from freeinference_pr.runtime import AgentRuntime
from freeinference_pr.store import Store, new_id, now
from freeinference_pr.tools import ToolContext, ToolRegistry, wrap_untrusted

RUNNER_ROLES = ("planner", "implementer", "verifier")

RUNNER_BLOCKER_SEVERITIES = frozenset({"critical", "high"})

# Bounded ingestion limits for operator-submitted review verdicts.
MAX_FINDINGS = 50
MAX_EVIDENCE_PER_FINDING = 20
MAX_TEXT_LEN = 4000

ROLE_MAPPING = {
    "planner": "repository_analyst",
    "implementer": "implementer",
    "verifier": "verifier",
}

# Tools available in Runner-managed mode, by Runner role. GitHub tools are
# excluded entirely: Runner owns GitHub interaction and reconciliation.
RUNNER_MODE_TOOLS = {
    "planner": frozenset({"repo_list_files", "repo_read_file", "git_status", "git_diff", "git_log"}),
    "implementer": frozenset({"repo_list_files", "repo_read_file", "git_status", "git_diff", "git_log"}),
    "verifier": frozenset({
        "repo_list_files", "repo_read_file", "git_status", "git_diff", "git_log", "run_commands",
    }),
}


class RunnerTaskPackage(BaseModel):
    """Mirror of Runner's TaskPackage (internal/workers/workers.go).

    Go marshals nil slices as JSON null, so every list field tolerates null
    and normalizes it to an empty list.
    """

    taskId: str
    attemptId: str
    attemptNo: int = 1
    projectId: str = ""
    repository: str = ""
    role: str
    title: str = ""
    goal: str = ""
    instructions: str = ""
    acceptance: list[str] = Field(default_factory=list)
    nonGoals: list[str] = Field(default_factory=list)
    baseSha: str = ""
    branch: str = ""
    worktree: str
    allowedPaths: list[str] = Field(default_factory=list)
    forbiddenPaths: list[str] = Field(default_factory=list)
    maxMinutes: int = 30
    budgetUsd: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    outputSchema: str = ""
    stopBehavior: str = ""
    idempotencyKey: str = ""
    packageFile: str = ""

    @field_validator(
        "acceptance", "nonGoals", "allowedPaths", "forbiddenPaths", "evidence",
        mode="before",
    )
    @classmethod
    def _null_list_to_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class RunnerBridgeError(Exception):
    pass


def load_package(path: str | Path) -> RunnerTaskPackage:
    data = json.loads(Path(path).read_text())
    return RunnerTaskPackage.model_validate(data)


def path_allowed(relative: str, allowed: list[str], forbidden: list[str]) -> bool:
    """Scope check against Runner's allowed/forbidden path lists (fnmatch globs).

    Absolute paths and any path that escapes the worktree (``..`` segments) are
    never allowed, regardless of the configured globs.
    """
    import fnmatch
    import os

    if not relative:
        return False
    if relative.startswith("/") or "\\" in relative and os.path.isabs(relative.replace("\\", "/")):
        return False
    normalized = os.path.normpath(relative.replace("\\", "/"))
    if normalized.startswith("..") or normalized == ".":
        return False
    for pattern in forbidden:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"/{normalized}", pattern):
            return False
    if not allowed:
        return True
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in allowed)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _git(worktree: Path, *argv: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(worktree), *argv], capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        raise RunnerBridgeError(f"git {' '.join(argv)} failed: {proc.stderr[-500:]}")
    return proc.stdout


def build_registry(role: str) -> ToolRegistry:
    """Registry restricted to the Runner role's read/verify tools.

    GitHub tools and approval-gated write tools are removed entirely: in
    Runner-managed mode no FI agent may touch GitHub, and privileged operations
    belong to Runner alone.
    """
    registry = ToolRegistry()
    permitted = RUNNER_MODE_TOOLS[role]
    for name in list(registry._tools):
        if name not in permitted:
            del registry._tools[name]
    return registry


def _runner_job(store: Store, pkg: RunnerTaskPackage) -> dict[str, Any]:
    """Persistent record for restart recovery, keyed by idempotency key."""
    key = f"runner-job:{pkg.idempotencyKey or pkg.attemptId}"
    existing = store.idempotency_get(key)
    if existing:
        try:
            return store.get_job(existing["job_id"])
        except KeyError:
            pass
    job = store.create_job(
        "runner_task", pkg.repository or "unknown", pkg.goal or pkg.title,
        issue_number=None,
    )
    store.idempotency_put(key, {"job_id": job["id"]})
    store.log_event(job["id"], "runner", "runner_task_accepted", {
        "taskId": pkg.taskId, "attemptId": pkg.attemptId, "role": pkg.role,
        "baseSha": pkg.baseSha, "branch": pkg.branch,
    })
    return job


async def run_runner_task(
    pkg: RunnerTaskPackage, config: FIConfig | None = None, store: Store | None = None
) -> dict[str, Any]:
    """Execute one Runner task package with FI agents. Returns the result dict."""
    config = config or FIConfig()
    own_store = store is None
    store = store or Store(config.state_path)
    try:
        return await _run_runner_task(pkg, config, store)
    finally:
        if own_store:
            store.close()


async def _run_runner_task(pkg: RunnerTaskPackage, config: FIConfig, store: Store) -> dict[str, Any]:
    from freeinference_pr.fi_provider import FreeInferenceClient

    result = _initial_result(pkg)
    idem_key = f"runner-result:{pkg.idempotencyKey or pkg.attemptId}"
    previous = store.idempotency_get(idem_key)
    if previous:
        previous["replayed"] = True
        return _finish(store, pkg, idem_key, previous)

    if pkg.role not in RUNNER_ROLES:
        result["state"] = "failed"
        result["error"] = f"role {pkg.role!r} not supported; supported: {list(RUNNER_ROLES)}"
        return _finish(store, pkg, idem_key, result)

    worktree = Path(pkg.worktree)
    if not (worktree / ".git").exists():
        result["state"] = "failed"
        result["error"] = f"worktree is not a git checkout: {pkg.worktree}"
        return _finish(store, pkg, idem_key, result)

    job = _runner_job(store, pkg)
    store.update_job(job["id"], status="running", stage="RUNNER_TASK")
    head = _git(worktree, "rev-parse", "HEAD").strip()
    result["head_sha"] = head
    if pkg.baseSha and head != pkg.baseSha:
        result["state"] = "failed"
        result["error"] = (
            f"candidate SHA moved: worktree HEAD {head} != package baseSha {pkg.baseSha}; "
            "refusing to act on a stale candidate"
        )
        store.log_event(job["id"], "runner", "sha_binding_failed", {"head": head, "base": pkg.baseSha})
        return _finish(store, pkg, idem_key, result)

    role = ROLE_MAPPING[pkg.role]
    registry = build_registry(pkg.role)
    client = FreeInferenceClient(config)
    runtime = AgentRuntime(client, registry, max_turns=config.max_agent_turns)
    ctx = ToolContext(
        job_id=job["id"], task_id=new_id("task"), role=role, worktree=worktree,
        repository=pkg.repository, base_ref=pkg.baseSha, head_branch=pkg.branch,
        env={},  # no credentials in Runner-managed mode; Runner owns GitHub
    )

    context_blocks: list[tuple[str, str]] = [
        ("Task package", json.dumps(redact_obj(pkg.model_dump()), default=str)),
    ]
    untrusted = {"Task package", "Acceptance evidence"}
    if pkg.evidence:
        context_blocks.append(("Acceptance evidence", "\n".join(pkg.evidence)))

    try:
        result = await asyncio.wait_for(
            _execute_role(pkg, role, runtime, ctx, context_blocks, untrusted, store, job, result, config),
            timeout=max(60.0, pkg.maxMinutes * 60.0),
        )
    except asyncio.TimeoutError:
        result["state"] = "cancelled"
        result["error"] = f"time budget exhausted ({pkg.maxMinutes} minutes)"
        store.log_event(job["id"], "runner", "runner_task_timeout", {"maxMinutes": pkg.maxMinutes})
        store.update_job(job["id"], status="cancelled")
        return _finish(store, pkg, idem_key, result)
    except asyncio.CancelledError:
        result["state"] = "cancelled"
        result["error"] = "cancelled"
        store.update_job(job["id"], status="cancelled")
        return _finish(store, pkg, idem_key, result)
    except Exception as exc:  # noqa: BLE001 - honest failure reporting
        result["state"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        store.update_job(job["id"], status="failed")
        store.log_event(job["id"], "runner", "runner_task_failed", {"error": result["error"]})
        return _finish(store, pkg, idem_key, result)
    finally:
        await client.close()

    return _finish(store, pkg, idem_key, result)


def _initial_result(pkg: RunnerTaskPackage) -> dict[str, Any]:
    return {
        "run_id": f"fi-{pkg.attemptId}",
        "task_id": pkg.taskId,
        "attempt_id": pkg.attemptId,
        "role": pkg.role,
        "state": "running",
        "head_sha": "",
        "commits": [],
        "result": "",
        "usage": {"tokens": 0, "known": True, "detail": "fi usage"},
        "error": "",
        "replayed": False,
    }


def _finish(store: Store, pkg: RunnerTaskPackage, idem_key: str, result: dict[str, Any]) -> dict[str, Any]:
    store.idempotency_put(idem_key, result)
    _write_result_file(pkg, result)
    return result


def _collect_schema(pkg: RunnerTaskPackage, result: dict[str, Any]) -> dict[str, Any]:
    """Render the result in Runner's Collect JSON shape (internal/workers/workers.go).

    Field names and semantics mirror Runner's Collect struct so the Runner-side
    collector can unmarshal this file directly. Extra diagnostic fields
    (taskId, attemptId, replayed) are additive and ignored by strict unmarshals.
    """
    usage = result.get("usage") or {}
    return {
        "runId": result.get("run_id", f"fi-{pkg.attemptId}"),
        "state": result.get("state", "unknown"),
        "headSha": result.get("head_sha", ""),
        "result": result.get("result", ""),
        "commits": result.get("commits", []),
        "usage": {
            "usd": usage.get("usd", 0),
            "known": bool(usage.get("known", False)),
            "tokens": usage.get("tokens", 0),
            "detail": usage.get("detail", "freeinference"),
        },
        "error": result.get("error", ""),
        "taskId": pkg.taskId,
        "attemptId": pkg.attemptId,
        "replayed": bool(result.get("replayed", False)),
    }


def _write_result_file(pkg: RunnerTaskPackage, result: dict[str, Any]) -> None:
    if not pkg.packageFile:
        return
    try:
        path = Path(pkg.packageFile + ".result.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _collect_schema(pkg, result)
        path.write_text(json.dumps(redact_obj(payload), indent=2, default=str))
    except OSError:
        pass


async def _execute_role(
    pkg: RunnerTaskPackage, role: str, runtime: AgentRuntime, ctx: ToolContext,
    context_blocks: list[tuple[str, str]], untrusted: set[str], store: Store,
    job: dict[str, Any], result: dict[str, Any], config: FIConfig,
) -> dict[str, Any]:
    if role == "repository_analyst":
        agent_result = await runtime.run(
            ctx, config.model_for(role),
            f"Analyze this task and produce a bounded implementation plan. "
            f"Goal: {pkg.goal or pkg.title}. Instructions: {pkg.instructions}",
            context_blocks, AnalysisReport, untrusted_sources=untrusted,
        )
        report = agent_result.structured
        if not isinstance(report, AnalysisReport):
            raise RunnerBridgeError("analyst reply did not parse as an AnalysisReport")
        result["result"] = report.model_dump_json()
        result["state"] = "succeeded"
    elif role == "implementer":
        agent_result = await runtime.run(
            ctx, config.model_for(role),
            f"Implement this task as bounded file edits plus regression tests. "
            f"Goal: {pkg.goal or pkg.title}. Instructions: {pkg.instructions}",
            context_blocks, EditsReport, untrusted_sources=untrusted,
        )
        edits_report = agent_result.structured
        if not isinstance(edits_report, EditsReport):
            raise RunnerBridgeError("implementer reply did not parse as an EditsReport")
        applied = _apply_scoped_edits(pkg, ctx.worktree, edits_report)
        commit_sha = _commit_scoped(pkg, ctx.worktree, applied,
                                    edits_report.commit_message or f"runner: {pkg.title}")
        if commit_sha:
            result["commits"] = [commit_sha]
            result["head_sha"] = commit_sha
        result["result"] = edits_report.model_dump_json()
        result["state"] = "succeeded"
    else:
        agent_result = await runtime.run(
            ctx, config.model_for(role),
            "Review the effective diff of this candidate for correctness and security. "
            "Run the relevant checks and report actual exit codes. Produce a review "
            "verdict: assessment, findings with severity and evidence, and missed paths. "
            "An empty blocker list means you found no blocking defect.",
            context_blocks, ReviewReport, untrusted_sources=untrusted,
        )
        review = agent_result.structured
        if not isinstance(review, ReviewReport):
            raise RunnerBridgeError("reviewer reply did not parse as a ReviewReport")
        diff = _candidate_diff(ctx.worktree, pkg.baseSha)
        blockers = [f for f in review.findings if f.severity in RUNNER_BLOCKER_SEVERITIES]
        result["result"] = json.dumps(redact_obj({
            "verdict": {
                "decision": "reject" if blockers else "approve",
                "candidate_sha": _git(ctx.worktree, "rev-parse", "HEAD").strip(),
                "base_sha": pkg.baseSha,
                "diff_digest": _sha256(diff),
                "evidence_kind": "fi_review",
                "reviewer_provenance": "fi_self_review",
                "grants_publication_authority": False,
                "assessment": review.assessment,
                "findings": [f.model_dump() for f in review.findings],
                "missed_paths": review.missed_paths,
                "unsupported_assumptions": review.unsupported_assumptions,
            }
        }))
        result["state"] = "succeeded"
        store.add_artifact(job["id"], "runner_review", ref=result["result"])

    store.log_event(job["id"], role, "runner_task_result", {
        "state": result["state"], "head_sha": result["head_sha"], "commits": result["commits"],
    })
    store.update_job(job["id"], status="completed", stage="COMPLETED")
    return result



def _candidate_diff(worktree: Path, base_sha: str) -> str:
    """Effective diff of the candidate under review.

    Runner's verifier packages carry the candidate SHA as ``baseSha`` (the SHA
    the review must be pinned to), so ``git diff <baseSha>`` is empty exactly
    when the binding is correct. In that case the candidate diff is the
    branch's changes against its trunk merge-base, falling back to the tip
    commit for detached single-commit candidates.
    """
    if base_sha:
        diff = _git(worktree, "diff", base_sha)
        if diff.strip():
            return diff
    head = _git(worktree, "rev-parse", "HEAD").strip()
    if base_sha and head != base_sha:
        return _git(worktree, "diff", base_sha)
    for trunk in ("main", "master"):
        try:
            merge_base = _git(worktree, "merge-base", "HEAD", trunk)
        except RunnerBridgeError:
            continue
        if merge_base.strip() and merge_base.strip() != head:
            return _git(worktree, "diff", f"{merge_base.strip()}..HEAD")
    parents = _git(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) >= 2:
        return _git(worktree, "diff", f"{parents[1]}..HEAD")
    return ""


def _apply_scoped_edits(
    pkg: RunnerTaskPackage, worktree: Path | None, edits_report: EditsReport
) -> list[str]:
    if worktree is None:
        raise RunnerBridgeError("no worktree bound")
    applied = []
    resolved_root = worktree.resolve()
    for edit in edits_report.edits:
        if not path_allowed(edit.path, pkg.allowedPaths, pkg.forbiddenPaths):
            raise RunnerBridgeError(
                f"edit path outside allowed scope: {edit.path} "
                f"(allowed={pkg.allowedPaths}, forbidden={pkg.forbiddenPaths})"
            )
        target = (worktree / edit.path).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError:
            raise RunnerBridgeError(f"edit path escapes worktree: {edit.path}") from None
        if edit.action == "delete":
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.content)
        applied.append(edit.path)
    return applied


def _commit_scoped(
    pkg: RunnerTaskPackage, worktree: Path, paths: list[str], message: str
) -> str | None:
    if not paths:
        return None
    _git(worktree, "add", "-A", "--", *paths)
    if not _git(worktree, "status", "--porcelain").strip():
        return None
    _git(worktree, "commit", "-m", message)
    return _git(worktree, "rev-parse", "HEAD").strip()


# ------------------------------------------------------------------------------
# External review handoff (no paid APIs)
# ------------------------------------------------------------------------------

VERDICT_TEMPLATE = """```json
{
  "decision": "approve | changes_requested | reject",
  "candidate_sha": "<sha this review applies to>",
  "findings": [
    {"title": "...", "severity": "critical|high|medium|low|info",
     "evidence": ["path:line or diff hunk"], "detail": "..."}
  ],
  "missed_paths": ["..."],
  "unsupported_assumptions": ["..."],
  "reviewer": "<your identity>",
  "reviewed_at": "<date>"
}
```"""


def build_review_packet(
    *,
    repository: str,
    task_identity: str,
    candidate_sha: str,
    base_sha: str = "",
    objective: str = "",
    acceptance: list[str] | None = None,
    diff: str = "",
    test_results: list[dict[str, Any]] | None = None,
    known_risks: list[str] | None = None,
    review_questions: list[str] | None = None,
    reviewer_hint: str = "",
) -> str:
    """Render a complete, copyable review packet for an external reviewer.

    Used for the manual handoff to ChatGPT (via the existing Kontrol review
    rendezvous), Gemini, or Mistral user-facing sessions. The packet contains
    everything a reviewer needs and ends with the exact structured verdict
    format the operator must paste back.
    """
    sections: list[str] = []
    sections.append(f"# Review request: {task_identity}")
    sections.append(f"Repository: `{repository}`")
    sections.append(f"Candidate SHA: `{candidate_sha}`")
    if base_sha:
        sections.append(f"Base SHA: `{base_sha}`")
    if objective:
        sections.append(f"\n## Objective\n{wrap_untrusted(objective, 'objective')}")
    if acceptance:
        sections.append("## Approved scope / acceptance criteria")
        sections.extend(f"- {item}" for item in acceptance)
    if diff:
        sections.append("## Effective diff")
        sections.append("```diff")
        sections.append(diff[:120000])
        sections.append("```")
    if test_results:
        sections.append("## Test and verification results (real executions)")
        for res in test_results:
            sections.append(
                f"- `{res.get('command', '')}` -> exit {res.get('exit_code', '?')}"
            )
    if known_risks:
        sections.append("## Known risks")
        sections.extend(f"- {risk}" for risk in known_risks)
    if review_questions:
        sections.append("## Review questions")
        sections.extend(f"{i}. {q}" for i, q in enumerate(review_questions, 1))
    if reviewer_hint:
        sections.append(f"\nReviewer transport: {reviewer_hint}")
    sections.append("## Required verdict format")
    sections.append(
        "Reply with ONLY the JSON object below (filled in). Findings must cite "
        "concrete evidence (path:line or diff hunk)."
    )
    sections.append(VERDICT_TEMPLATE)
    return "\n\n".join(sections) + "\n"


def import_manual_review(
    store: Store,
    job_id: str,
    verdict_json: str,
    *,
    candidate_sha: str,
    reviewer: str,
    transport: str,
) -> dict[str, Any]:
    """Record an operator-submitted external review verdict.

    The submission is stored as OPERATOR-PROVIDED review evidence: it records
    who the reviewer claims to be and which transport carried it, but it is not
    an authenticated automated model response. It never grants authority to
    publish or merge; Runner's qualification and operator-approval policies
    still apply.
    """
    if len(verdict_json) > 1_000_000:
        raise RunnerBridgeError("review submission exceeds the 1 MB ingestion bound")
    start, end = verdict_json.find("{"), verdict_json.rfind("}")
    if start == -1 or end <= start:
        raise RunnerBridgeError("review submission contains no JSON verdict object")
    try:
        verdict = json.loads(verdict_json[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RunnerBridgeError(f"invalid verdict JSON: {exc}") from None
    decision = str(verdict.get("decision", "")).lower()
    if decision not in ("approve", "changes_requested", "reject"):
        raise RunnerBridgeError(
            f"verdict decision must be approve|changes_requested|reject, got {decision!r}"
        )
    submitted_sha = str(verdict.get("candidate_sha", "")).strip()
    if submitted_sha and candidate_sha and submitted_sha != candidate_sha:
        raise RunnerBridgeError(
            f"verdict is for candidate {submitted_sha}, but the current candidate is "
            f"{candidate_sha}; stale reviews are rejected — re-review the current candidate"
        )
    findings = _bounded_findings(verdict.get("findings") or [])
    record = {
        "id": new_id("rev"),
        "job_id": job_id,
        "kind": "external_review",
        "source": "operator",
        "provenance": "operator_provided",
        "grants_publication_authority": False,
        "transport": transport,
        "reviewer_claimed": str(verdict.get("reviewer", reviewer))[:MAX_TEXT_LEN],
        "decision": decision,
        "candidate_sha": candidate_sha,
        "verdict": redact_obj(verdict),
        "recorded_at": now(),
    }
    store.add_artifact(job_id, "external_review", ref=json.dumps(record, default=str))
    store.log_event(job_id, "operator", "external_review_recorded", {
        "decision": decision, "transport": transport, "candidate_sha": candidate_sha,
        "note": "operator-provided evidence, not an authenticated automated response",
    })
    for finding in findings:
        store.add_finding(
            job_id, f"external_review:{transport}", finding["title"],
            finding["severity"], finding["evidence"], finding["detail"],
        )
    return record


def _bounded_findings(raw: Any) -> list[dict[str, Any]]:
    """Schema-validate and bound operator-submitted findings.

    Oversized or malformed submissions are truncated/dropped, never trusted
    wholesale: ingestion is bounded regardless of what the reviewer returns.
    """
    if not isinstance(raw, list):
        return []
    bounded: list[dict[str, Any]] = []
    for item in raw[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        evidence_raw = item.get("evidence", []) or []
        if not isinstance(evidence_raw, list):
            evidence_raw = []
        bounded.append({
            "title": title[:MAX_TEXT_LEN],
            "severity": str(item.get("severity", "medium"))[:32],
            "evidence": [str(e)[:MAX_TEXT_LEN] for e in evidence_raw[:MAX_EVIDENCE_PER_FINDING]],
            "detail": str(item.get("detail", ""))[:MAX_TEXT_LEN],
        })
    return bounded
