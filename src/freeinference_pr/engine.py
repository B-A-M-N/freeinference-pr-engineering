"""The deterministic PR engineering engine.

Owns scheduling and lifecycle; agents own analysis and proposals. Stage
transitions are code, not model decisions. Parallel independent tasks run
concurrently within the FreeInference concurrency limit; dependent tasks wait.
Privileged (remote) operations are executed only through the approval gate.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from freeinference_pr.agent_models import (
    AnalysisReport,
    EditsReport,
    FileEdit,
    PackageReport,
    ReviewReport,
    TestPlan,
    VerificationReport,
)
from freeinference_pr.approvals import ApprovalGate, ApprovalRequired
from freeinference_pr.config import FIConfig
from freeinference_pr.fi_provider import FreeInferenceClient
from freeinference_pr.runtime import AgentRuntime
from freeinference_pr.store import Store
from freeinference_pr.tools import ToolContext, ToolRegistry, ToolError

REVIEW_FINDING_BLOCKERS = {"critical", "high"}


class Worktree:
    """One isolated checkout for a job (primary) or an implementation unit."""

    def __init__(self, path: Path, repository: str, base_ref: str, branch: str) -> None:
        self.path = path
        self.repository = repository
        self.base_ref = base_ref
        self.branch = branch

    def git(self, *argv: str, timeout: int = 600) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *argv], capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(argv)} failed: {proc.stderr[-1500:]}")
        return proc.stdout

    def apply_edits(self, edits: list[FileEdit]) -> list[str]:
        applied = []
        for edit in edits:
            target = (self.path / edit.path).resolve()
            if not str(target).startswith(str(self.path.resolve())):
                raise RuntimeError(f"edit path escapes worktree: {edit.path}")
            if edit.action == "delete":
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(edit.content)
            applied.append(edit.path)
        return applied

    def commit(self, message: str, paths: list[str] | None = None) -> str | None:
        if paths:
            tracked = set(self.git("ls-files").split())
            stage = [p for p in paths if (self.path / p).exists() or p in tracked]
            if stage:
                self.git("add", "-A", "--", *stage)
        else:
            self.git("add", "-A")
        status = self.git("status", "--porcelain").strip()
        if not status:
            return None
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


def prepare_worktree(
    workspace_root: Path, job_id: str, repository: str, base_ref: str, branch: str
) -> Worktree:
    repo_dir = workspace_root / job_id / "repo"
    if repo_dir.exists():
        subprocess.run(["rm", "-rf", str(repo_dir.parent)], check=True)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", f"https://github.com/{repository}.git", str(repo_dir)],
        check=True, capture_output=True, timeout=600,
    )
    worktree = Worktree(repo_dir, repository, base_ref, branch)
    if base_ref:
        worktree.git("checkout", base_ref)
    worktree.git("switch", "-c", branch)
    return worktree


class Engine:
    def __init__(
        self,
        config: FIConfig,
        store: Store,
        client: FreeInferenceClient,
        registry: ToolRegistry | None = None,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.registry = registry or ToolRegistry()
        self.runtime = runtime or AgentRuntime(client, self.registry, max_turns=config.max_agent_turns)
        self.gate = ApprovalGate(store)

    # ------------------------------------------------------------------ planning

    def plan(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        """Deterministic minimum-role task graph for the job kind."""
        job_id = job["id"]
        tasks: list[dict[str, Any]] = []
        analyze = self.store.add_task(job_id, "repository_analyst", "Analyze repository and request")
        tasks.append(analyze)
        if job["kind"] == "review_pr":
            tasks.append(self.store.add_task(job_id, "code_reviewer", "Independent code review", [analyze["id"]]))
            tasks.append(self.store.add_task(job_id, "verifier", "Run tests and interpret CI", [analyze["id"]]))
            tasks.append(self.store.add_task(job_id, "pr_manager", "Prepare review report", 
                                            [t["id"] for t in tasks[1:]]))
        else:
            tasks.append(self.store.add_task(job_id, "implementer", "Implement changes", [analyze["id"]]))
            implement_ids = [tasks[-1]["id"]]
            tasks.append(self.store.add_task(job_id, "verifier", "Execute tests", implement_ids))
            tasks.append(self.store.add_task(job_id, "code_reviewer", "Independent review", implement_ids))
            tasks.append(self.store.add_task(job_id, "security_reviewer", "Security review", implement_ids))
            tasks.append(self.store.add_task(job_id, "integrator", "Integrate and verify combined changes",
                                            [t["id"] for t in tasks[-3:]]))
            tasks.append(self.store.add_task(job_id, "pr_manager", "Prepare PR package",
                                            [tasks[-1]["id"]]))
        self.store.update_job(job_id, plan_json=json.dumps([t["id"] for t in tasks]))
        return tasks

    # ------------------------------------------------------------------ execution

    async def run_job(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        if job["status"] in ("completed", "published", "cancelled"):
            return job["status"]
        try:
            if job["kind"] == "review_pr":
                final = await self._run_review_job(job)
            else:
                final = await self._run_implement_job(job)
            return final
        except ApprovalRequired as exc:
            self.store.update_job(job_id, status="awaiting_approval", stage="AWAITING_APPROVAL")
            self.store.log_event(job_id, "engine", "awaiting_approval", {"message": str(exc)})
            return "awaiting_approval"
        except asyncio.CancelledError:
            self.store.update_job(job_id, status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - job failure is recorded, not swallowed
            self.store.update_job(job_id, status="failed")
            self.store.log_event(job_id, "engine", "job_failed", {"error": f"{type(exc).__name__}: {exc}"})
            return "failed"

    # ------------------------------------------------------------- review jobs

    async def _run_review_job(self, job: dict[str, Any]) -> str:
        job_id = job["id"]
        self.store.update_job(job_id, status="running", stage="ANALYSIS")
        evidence = await self._fetch_pr_evidence(job)
        analysis = await self._agent_task(
            job, "repository_analyst", "repository_analyst",
            f"Analyze {job['repository']} PR #{job['pr_number']}. Objective: {job['objective']}",
            [("Repository evidence", json.dumps(evidence, default=str))],
            AnalysisReport, untrusted={"Repository evidence"},
        )
        for draft in analysis.findings:
            self.store.add_finding(job_id, "analysis", draft.title, draft.severity, draft.evidence, draft.detail)
        self.store.add_artifact(job_id, "analysis", ref=json.dumps(analysis.model_dump(), default=str))

        self.store.update_job(job_id, stage="REVIEW")
        review, security = await asyncio.gather(
            self._agent_task(
                job, "code_reviewer", "code_reviewer",
                f"Independently review the PR diff for {job['repository']}#{job['pr_number']}.",
                [("Diff and files", json.dumps(evidence, default=str))],
                ReviewReport, untrusted={"Diff and files"},
            ),
            self._agent_task(
                job, "security_reviewer", "security_reviewer",
                f"Security review of the PR diff for {job['repository']}#{job['pr_number']}: "
                "trust boundaries, input handling, secrets, authz.",
                [("Diff and files", json.dumps(evidence, default=str))],
                ReviewReport, untrusted={"Diff and files"},
            ),
        )
        for source, report in (("code_review", review), ("security_review", security)):
            for draft in report.findings:
                self.store.add_finding(job_id, source, draft.title, draft.severity, draft.evidence, draft.detail)
            self.store.add_artifact(job_id, source, ref=json.dumps(report.model_dump(), default=str))

        self.store.update_job(job_id, stage="VERIFICATION")
        ci = evidence.get("ci_status") if isinstance(evidence, dict) else None
        verification = await self._agent_task(
            job, "verifier", "verifier",
            f"Interpret the CI status and checks for {job['repository']}#{job['pr_number']}; "
            "state which findings the evidence resolves. Do not run anything; interpret only.",
            [("CI evidence", json.dumps(ci, default=str)), ("Findings", json.dumps(
                self.store.findings_for_job(job_id), default=str))],
            VerificationReport, untrusted={"CI evidence"},
        )
        self.store.add_verification(
            job_id, None, ["ci-interpretation"], [], bool(verification.passed),
            notes=verification.interpretation,
        )

        self.store.update_job(job_id, stage="PACKAGING")
        package = await self._agent_task(
            job, "pr_manager", "pr_manager",
            "Draft the review report for the PR: findings with severity and evidence, "
            "verification interpretation, and a recommended action. Publishing a comment "
            "requires approval; set publish=true only if the user asked for it.",
            [("Mission record", self._mission_json(job_id))],
            PackageReport,
        )
        self.store.add_artifact(job_id, "pr_draft", ref=package.body_markdown)
        self.store.update_job(job_id, stage="AWAITING_APPROVAL")
        if package.publish:
            self.gate.require_approved(job_id, "pr_comment", {
                "repository": job["repository"], "pr_number": job["pr_number"],
            })
            await self._publish_comment(job, package.body_markdown)
            self.store.update_job(job_id, status="published", stage="PUBLISHED")
            return "published"
        self.store.update_job(job_id, status="completed", stage="COMPLETED")
        return "completed"

    # ---------------------------------------------------------- implement jobs

    async def _run_implement_job(self, job: dict[str, Any]) -> str:
        job_id = job["id"]
        self.store.update_job(job_id, status="running", stage="ANALYSIS")
        worktree = self._ensure_worktree(job)
        issue_evidence = await self._fetch_request_evidence(job)
        analysis = await self._agent_task(
            job, "repository_analyst", "repository_analyst",
            f"Analyze {job['repository']} for this request: {job['objective']}. "
            "Determine affected components and split the work into minimal implementation units "
            "(one unit unless the change clearly needs several).",
            [("Request evidence", json.dumps(issue_evidence, default=str))],
            AnalysisReport, untrusted={"Request evidence"},
        )
        for draft in analysis.findings:
            self.store.add_finding(job_id, "analysis", draft.title, draft.severity, draft.evidence, draft.detail)
        self.store.add_artifact(job_id, "analysis", ref=json.dumps(analysis.model_dump(), default=str))

        cycles = 0
        stage = "IMPLEMENTATION"
        while True:
            self.store.update_job(job_id, stage=stage)
            open_findings = [
                f for f in self.store.findings_for_job(job_id)
                if f["status"] in ("OPEN", "ASSIGNED") and f["severity"] in REVIEW_FINDING_BLOCKERS
            ]
            if stage == "CORRECTION":
                cycles += 1
                if cycles > self.config.max_correction_cycles:
                    self.store.update_job(job_id, status="blocked",
                                          stage="BLOCKED")
                    self.store.log_event(job_id, "engine", "blocked", {"reason": "correction cycle bound reached"})
                    return "blocked"
            if stage == "IMPLEMENTATION" or open_findings:
                unit_list = analysis.implementation_units or []
                await self._implement_units(job, unit_list, worktree, open_findings)
                if len(unit_list) > 1:
                    await self._integration_check(job, worktree)
                if open_findings:
                    for finding in open_findings:
                        self.store.update_finding(finding["id"], status="CORRECTED")

            self.store.update_job(job_id, stage="TESTING")
            test_plan = await self._agent_task(
                job, "verifier", "verifier",
                "Choose the minimal relevant test/lint/build commands for the changed code, then run them.",
                [("Worktree", str(worktree.path)), ("Objective", job["objective"])],
                TestPlan,
            )
            test_results = await self._execute_commands(job, worktree, test_plan.commands)
            exit_codes = [r["exit_code"] for r in test_results]

            self.store.update_job(job_id, stage="REVIEW")
            diff = worktree.git("diff", worktree.base_ref or "HEAD") if worktree.base_ref else worktree.git("diff", "HEAD")
            review, security = await asyncio.gather(
                self._agent_task(
                    job, "code_reviewer", "code_reviewer",
                    "Independently review the effective diff for correctness and regressions.",
                    [("Effective diff", diff)], ReviewReport, untrusted={"Effective diff"},
                ),
                self._agent_task(
                    job, "security_reviewer", "security_reviewer",
                    "Security review of the effective diff (trust boundaries, input handling, secrets).",
                    [("Effective diff", diff)], ReviewReport, untrusted={"Effective diff"},
                ),
            )
            new_blockers = []
            for source, report in (("code_review", review), ("security_review", security)):
                for draft in report.findings:
                    finding = self.store.add_finding(job_id, source, draft.title, draft.severity, draft.evidence, draft.detail)
                    if draft.severity in REVIEW_FINDING_BLOCKERS:
                        new_blockers.append(finding)
            self.store.add_artifact(job_id, "review", ref=json.dumps(review.model_dump(), default=str))
            self.store.add_artifact(job_id, "security_review", ref=json.dumps(security.model_dump(), default=str))

            self.store.update_job(job_id, stage="VERIFICATION")
            verification = await self._agent_task(
                job, "verifier", "verifier",
                "Verify the combined changes: interpret the real test results below and state "
                "whether each finding is resolved. Facts only.",
                [("Test results", json.dumps(test_results, default=str)),
                 ("Findings", json.dumps(self.store.findings_for_job(job_id), default=str))],
                VerificationReport, untrusted={"Test results"},
            )
            passed = (all(code == 0 for code in exit_codes) if exit_codes else False) and bool(verification.passed)
            commit_sha = worktree.git("rev-parse", "HEAD").strip()
            self.store.add_verification(
                job_id, None, test_plan.commands, exit_codes, passed, commit_sha=commit_sha,
                notes=verification.interpretation,
            )
            if not passed:
                self.store.update_job(job_id, status="blocked", stage="BLOCKED")
                self.store.log_event(job_id, "engine", "blocked", {"reason": "verification failed"})
                return "blocked"

            if new_blockers:
                stage = "CORRECTION"
                for finding in new_blockers:
                    self.store.update_finding(finding["id"], status="ASSIGNED")
                continue
            break

        self.store.update_job(job_id, stage="PACKAGING")
        package = await self._agent_task(
            job, "pr_manager", "pr_manager",
            "Prepare the PR package: accurate title, description, verification evidence, "
            "and the list of commits. Set publish=true so the system requests authorization "
            "to push the branch and open the PR.",
            [("Mission record", self._mission_json(job_id))],
            PackageReport,
        )
        self.store.add_artifact(job_id, "pr_draft", ref=package.body_markdown)

        self.store.update_job(job_id, stage="AWAITING_APPROVAL")
        if package.publish:
            self.gate.require_approved(job_id, "publish_branch_and_pr", {
                "repository": job["repository"],
                "branch": worktree.branch,
                "commit": worktree.git("rev-parse", "HEAD").strip(),
            })
            await self._publish_pr(job, worktree, package)
            self.store.update_job(job_id, status="published", stage="PUBLISHED")
            return "published"
        self.store.update_job(job_id, status="completed", stage="COMPLETED")
        return "completed"

    async def _implement_units(
        self, job: dict[str, Any], units: list[Any], worktree: Worktree, findings: list[dict[str, Any]]
    ) -> None:
        job_id = job["id"]
        if not units:
            return
        if len(units) == 1:
            await self._implement_one(job, units[0], worktree, findings)
            return
        unit_worktrees = [self._unit_worktree(job, worktree, index) for index in range(len(units))]
        await asyncio.gather(
            *[
                self._implement_one(job, unit, unit_worktrees[index], findings)
                for index, unit in enumerate(units)
            ]
        )
        for index, unit_worktree in enumerate(unit_worktrees):
            sha = unit_worktree.git("rev-parse", "HEAD").strip()
            base_sha = unit_worktree.git("rev-parse", "HEAD~1").strip() if unit_worktree.git("rev-list", "--count", "HEAD").strip() != "1" else None
            try:
                if base_sha:
                    worktree.git("cherry-pick", sha)
                else:
                    patch = unit_worktree.git("diff", "HEAD~1..HEAD") if unit_worktree.git("rev-list", "--count", "HEAD").strip() != "1" else unit_worktree.git("diff", "--root", "HEAD")
                    proc = subprocess.run(
                        ["git", "-C", str(worktree.path), "apply", "-"],
                        input=patch, capture_output=True, text=True, timeout=120,
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(f"patch apply failed: {proc.stderr[-800:]}")
                    worktree.git("add", "-A")
                    worktree.git("commit", "-m", f"integrate unit {index}")
            except Exception as exc:  # noqa: BLE001 - integration conflicts are honest defects
                self.store.add_finding(
                    job_id, "integration", f"Unit {index} ({unit_worktree.branch}) failed to integrate: {exc}",
                    "high", [str(unit_worktree.path)], str(exc)[:1000],
                )
                raise
        self.store.log_event(job_id, "engine", "units_integrated", {"units": len(units)})

    def _unit_worktree(self, job: dict[str, Any], primary: Worktree, index: int) -> Worktree:
        path = primary.path.parent / f"unit-{index}"
        branch = f"fi-pr/{job['id']}-unit{index}"
        if not path.exists():
            primary.git("worktree", "add", "-b", branch, str(path))
        return Worktree(path, primary.repository, primary.base_ref, branch)

    async def _integration_check(self, job: dict[str, Any], worktree: Worktree) -> None:
        await self._agent_task(
            job, "integrator", "integrator",
            "Multiple implementation units were combined into one worktree. Run the combined "
            "test suite and check the units compose (no conflicting assumptions, duplicate "
            "definitions, or broken imports). Report integration defects.",
            [("Worktree", str(worktree.path))], VerificationReport,
        )

    async def _implement_one(
        self, job: dict[str, Any], unit: Any, worktree: Worktree,
        findings: list[dict[str, Any]], index: int = 0,
    ) -> None:
        job_id = job["id"]
        context = [
            ("Objective", job["objective"]),
            ("Unit", f"{unit.title}: {unit.guidance}\nPaths: {', '.join(unit.paths) or 'agent decides'}"),
            ("Findings to correct", json.dumps(findings, default=str)),
        ]
        edits_report = await self._agent_task(
            job, "implementer", "implementer",
            "Implement the unit as a bounded set of file edits plus regression tests. "
            "Preserve unrelated changes and existing style.",
            context, EditsReport,
        )
        applied = worktree.apply_edits(edits_report.edits)
        commit_message = edits_report.commit_message or f"implement: {unit.title}"
        commit_sha = worktree.commit(commit_message, paths=applied)
        if commit_sha:
            self.store.invalidate_verifications(job_id)
            self.store.add_artifact(job_id, "commit", ref=commit_sha)
            self.store.log_event(job_id, "implementer", "edits_committed",
                                 {"unit": unit.title, "commit": commit_sha, "files": applied})
        if edits_report.regression_tests:
            self.store.add_artifact(job_id, "regression_tests", ref=json.dumps(edits_report.regression_tests))

    # ------------------------------------------------------------------ helpers

    def _ensure_worktree(self, job: dict[str, Any]) -> Worktree:
        existing = self.store.idempotency_get(f"worktree:{job['id']}")
        if existing and Path(existing["path"]).exists():
            return Worktree(Path(existing["path"]), job["repository"],
                            existing["base_ref"], existing["branch"])
        worktree = prepare_worktree(
            self.config.workspace_root, job["id"], job["repository"],
            "", f"fi-pr/{job['id']}",
        )
        self.store.idempotency_put(
            f"worktree:{job['id']}",
            {"path": str(worktree.path), "base_ref": worktree.base_ref, "branch": worktree.branch},
        )
        return worktree

    async def _agent_task(
        self, job: dict[str, Any], role: str, _role_key: str, task: str,
        context_blocks: list[tuple[str, str]], output_model: type,
        untrusted: set[str] | None = None,
    ) -> Any:
        task_row = self.store.add_task(job["id"], role, task[:120])
        self.store.update_task(task_row["id"], status="running", started_at=time.time(),
                               heartbeat_at=time.time())
        ctx = ToolContext(
            job_id=job["id"], task_id=task_row["id"], role=role,
            worktree=self._worktree_if_any(job), repository=job["repository"],
            approvals=self.gate, env={"GITHUB_TOKEN": self.config.github_token,
                                      "GITHUB_API_URL": self.config.github_api},
        )
        try:
            result = await asyncio.wait_for(
                self.runtime.run(
                    ctx, self.config.model_for(role), task, context_blocks,
                    output_model, untrusted_sources=untrusted,
                ),
                timeout=self.config.task_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self.store.update_task(task_row["id"], status="failed", error="task timeout (stall detection)")
            raise
        self.store.update_task(
            task_row["id"], status="done", finished_at=time.time(),
            result_json=json.dumps(
                {"turns": result.turns, "tool_calls": result.tool_calls_made, "usage": result.usage},
                default=str,
            ),
        )
        self.store.log_event(job["id"], role, "agent_result", {
            "task_id": task_row["id"], "turns": result.turns, "usage": result.usage,
            "tools": [c["tool"] for c in result.tool_calls_made],
        })
        return result.structured

    def _worktree_if_any(self, job: dict[str, Any]) -> Path | None:
        existing = self.store.idempotency_get(f"worktree:{job['id']}")
        if existing and Path(existing["path"]).exists():
            return Path(existing["path"])
        return None

    async def _execute_commands(self, job: dict[str, Any], worktree: Worktree, commands: list[str]) -> list[dict[str, Any]]:
        from freeinference_pr.tools import _run_shell

        return [_run_shell(worktree.path, command) for command in commands[:10]]

    async def _fetch_pr_evidence(self, job: dict[str, Any]) -> dict[str, Any]:
        ctx = ToolContext(job_id=job["id"], task_id="intake", role="system",
                          repository=job["repository"],
                          env={"GITHUB_TOKEN": self.config.github_token,
                               "GITHUB_API_URL": self.config.github_api})
        evidence: dict[str, Any] = {}
        for kind, kwargs in [
            ("pr", {"number": job["pr_number"]}),
            ("pr_diff", {"number": job["pr_number"]}),
            ("pr_files", {"number": job["pr_number"]}),
        ]:
            try:
                evidence[kind] = await self.registry.call(ctx, "github_get", {"kind": kind, **kwargs})
            except Exception as exc:  # noqa: BLE001 - evidence is best-effort
                evidence[kind] = {"error": f"{type(exc).__name__}: {exc}"}
        self.store.add_artifact(job["id"], "pr_evidence", ref=json.dumps(
            {k: "ok" if "error" not in v else v.get("error") for k, v in evidence.items()}
        ))
        return evidence

    async def _fetch_request_evidence(self, job: dict[str, Any]) -> dict[str, Any]:
        ctx = ToolContext(job_id=job["id"], task_id="intake", role="system",
                          repository=job["repository"],
                          env={"GITHUB_TOKEN": self.config.github_token,
                               "GITHUB_API_URL": self.config.github_api})
        evidence: dict[str, Any] = {"repository": job["repository"], "objective": job["objective"]}
        if job.get("issue_number"):
            try:
                evidence["issue"] = await self.registry.call(
                    ctx, "github_get", {"kind": "issue", "number": job["issue_number"]}
                )
                evidence["issue_comments"] = await self.registry.call(
                    ctx, "github_get", {"kind": "issue_comments", "number": job["issue_number"]}
                )
            except Exception as exc:  # noqa: BLE001
                evidence["issue"] = {"error": f"{type(exc).__name__}: {exc}"}
        return evidence

    async def _publish_comment(self, job: dict[str, Any], body: str) -> None:
        ctx = ToolContext(job_id=job["id"], task_id="publish", role="pr_manager",
                          repository=job["repository"], approvals=self.gate,
                          env={"GITHUB_TOKEN": self.config.github_token,
                               "GITHUB_API_URL": self.config.github_api})
        await self.registry.call(ctx, "github_pr_comment",
                                 {"number": job["pr_number"], "body": body})

    async def _publish_pr(self, job: dict[str, Any], worktree: Worktree, package: PackageReport) -> None:
        ctx = ToolContext(job_id=job["id"], task_id="publish", role="pr_manager",
                          repository=job["repository"], head_branch=worktree.branch,
                          approvals=self.gate,
                          env={"GITHUB_TOKEN": self.config.github_token,
                               "GITHUB_API_URL": self.config.github_api})
        commit_sha = worktree.git("rev-parse", "HEAD").strip()
        await self.registry.call(ctx, "github_create_branch",
                                 {"branch": worktree.branch, "from_ref": commit_sha})
        files = []
        diff_files = worktree.git("diff", "--name-only", worktree.base_ref or "HEAD").split()
        for path in diff_files[:50]:
            target = worktree.path / path
            if target.is_file():
                files.append({"path": path, "content": target.read_text(errors="replace")})
        if files:
            await self.registry.call(ctx, "github_commit_files",
                                     {"branch": worktree.branch, "message": package.title, "files": files})
        base = job.get("pr_number")
        base_branch = ""
        if base:
            pr = await self.registry.call(ctx, "github_get", {"kind": "pr", "number": base})
            base_branch = (pr.get("base") or {}).get("ref", "") if isinstance(pr, dict) else ""
        await self.registry.call(ctx, "github_create_pr", {
            "title": package.title, "body": package.body_markdown,
            "head": worktree.branch, "base": base_branch or "main",
        })

    def _mission_json(self, job_id: str) -> str:
        return json.dumps({
            "job": self.store.get_job(job_id),
            "findings": self.store.findings_for_job(job_id),
            "verifications": self.store.verifications_for_job(job_id),
            "artifacts": self.store.artifacts_for_job(job_id),
        }, default=str)
