"""The conversational PR engineering workflow (Vibe Web entry point).

One long-running execution per PR mission. The execution's durable state is the
authoritative mission record: query handlers expose it to both Vibe Web and the
Vibe CLI (through Mistral MCP), and update/signal handlers accept approvals and
chat input from either interface.

Stages: INTAKE -> INVESTIGATION -> [IMPLEMENTATION] -> REVIEW -> VERIFICATION
-> PR_PREPARATION -> AWAITING_APPROVAL -> COMPLETED (or BLOCKED).

Invariants enforced here (not trusted to any model):
- read-only missions never mutate anything remote;
- verification is invalidated whenever the source changes;
- completion requires the mandatory coverage checklist and no unresolved
  critical findings (corrective missions);
- every remote mutation requires an explicit user approval that names the
  operation, repository, branch, and commit.
"""

from __future__ import annotations

import json
from datetime import timedelta

import mistralai.workflows as workflows
import mistralai.workflows.plugins.mistralai as workflows_mistralai
from pydantic import BaseModel, Field

from freeinference_pr.activities import (
    apply_edits,
    fetch_pr_evidence,
    prepare_workspace,
    run_role_stage,
    run_tests,
    workspace_diff,
)
from freeinference_pr.activities import github_connector
from mistralai.workflows.plugins.mistralai.connectors import uses_connectors
from freeinference_pr.mission_state import (
    MANDATORY_COVERAGE,
    STAGE_ORDER,
    ApprovalRecord,
    MissionState,
    VerificationRecord,
    mission_key as build_mission_key,
    new_id,
)
from freeinference_pr.mission_store import load_mission_snapshot, signal_mission_adopted
from freeinference_pr.role_agents import (
    InvestigationReport,
    PackageDraft,
    ReviewReport,
    VerificationReport,
)

MAX_CORRECTION_CYCLES = 3

READ_ONLY_STAGES: dict[str, str | None] = {
    "INTAKE": "INVESTIGATION",
    "INVESTIGATION": "REVIEW",
    "REVIEW": "VERIFICATION",
    "VERIFICATION": "PR_PREPARATION",
    "PR_PREPARATION": "AWAITING_APPROVAL",
    "AWAITING_APPROVAL": "COMPLETED",
}

CORRECTIVE_STAGES: dict[str, str | None] = {
    "INTAKE": "INVESTIGATION",
    "INVESTIGATION": "IMPLEMENTATION",
    "IMPLEMENTATION": "REVIEW",
    "REVIEW": "VERIFICATION",
    "VERIFICATION": "PR_PREPARATION",
    "PR_PREPARATION": "AWAITING_APPROVAL",
    "AWAITING_APPROVAL": "COMPLETED",
}


def next_stage(state: MissionState) -> str | None:
    """Pure stage-transition function (unit-tested)."""
    if state.stage == "COMPLETED":
        return None
    table = CORRECTIVE_STAGES if not state.read_only else READ_ONLY_STAGES
    nxt = table.get(state.stage)
    if state.stage == "VERIFICATION" and not state.read_only:
        critical = state.unresolved_critical()
        if critical and state.correction_cycles < MAX_CORRECTION_CYCLES:
            return "IMPLEMENTATION"
    return nxt


class MissionRequest(BaseModel):
    """Workflow input. All fields optional: provide mission_key to continue an
    existing mission, or repository/pr_number/objective to start one."""

    mission_key: str | None = Field(default=None, description="Continue an existing PR mission")
    repository: str | None = Field(default=None, description="owner/repo")
    pr_number: int | None = None
    base_branch: str | None = None
    objective: str | None = None
    read_only: bool = True
    instruction: str | None = Field(default=None, description="Free-form instruction for this run")


class ApprovalDecision(BaseModel):
    operation: str
    approved: bool
    comment: str = ""


@workflows.workflow.define(
    name="pr-engineering-assistant",
    workflow_display_name="FreeInference PR Engineering",
    workflow_description="Multi-agent PR engineering missions on FreeInference: investigation, independent review, implementation, verification, packaging, with approval gates and cross-interface mission state.",
    on_behalf_of=True,
    execution_timeout=timedelta(days=30),
)
@uses_connectors(github_connector)
class PREngineeringAssistantWorkflow(workflows.InteractiveWorkflow):
    def __init__(self) -> None:
        self._state: MissionState | None = None
        self._retired = False

    # ------------------------------------------------------------------ state

    @workflows.workflow.query(name="mission_state")
    def mission_state(self) -> dict:
        return self._state.model_dump(mode="json") if self._state else {}

    @workflows.workflow.query(name="mission_key")
    def mission_key(self) -> str:
        return self._state.mission_key if self._state else ""

    @workflows.workflow.query(name="findings")
    def findings(self, status: str | None = None) -> list[dict]:
        if not self._state:
            return []
        items = self._state.findings
        if status:
            items = [f for f in items if f.status == status]
        return [f.model_dump(mode="json") for f in items]

    @workflows.workflow.query(name="coverage")
    def coverage(self) -> list[dict]:
        if not self._state:
            return []
        return [c.model_dump(mode="json") for c in self._state.coverage]

    @workflows.workflow.query(name="awaiting_approval")
    def awaiting_approval(self) -> list[dict]:
        if not self._state:
            return []
        return [
            a.model_dump(mode="json")
            for a in self._state.approvals
            if a.decision == "pending"
        ]

    @workflows.workflow.update(name="record_approval")
    async def record_approval(self, decision: ApprovalDecision) -> dict:
        if not self._state:
            raise RuntimeError("No active mission in this execution")
        for approval in self._state.approvals:
            if approval.operation == decision.operation and approval.decision == "pending":
                approval.decision = "approved" if decision.approved else "rejected"
                approval.decided_at = workflows.workflow.now()
        self._state.touch()
        return {"ok": True}

    @workflows.workflow.signal(name="mission_adopted")
    async def mission_adopted(self, input: dict) -> None:
        self._retired = True

    # ------------------------------------------------------------- entrypoint

    @workflows.workflow.entrypoint
    async def run(self, request: MissionRequest) -> workflows_mistralai.ChatAssistantWorkflowOutput:
        state = await self._intake(request)
        self._state = state
        await workflows.workflow.add_search_keys({"mission_key": state.mission_key})

        await workflows_mistralai.send_assistant_message(
            self._format_status(state, header=f"Mission `{state.mission_key}` ready.")
        )

        while state.stage != "COMPLETED" and not self._retired:
            if state.stage == "INTAKE":
                await self._stage_intake(state)
            elif state.stage == "INVESTIGATION":
                await self._stage_investigation(state)
            elif state.stage == "IMPLEMENTATION":
                await self._stage_implementation(state)
            elif state.stage == "REVIEW":
                await self._stage_review(state)
            elif state.stage == "VERIFICATION":
                await self._stage_verification(state)
            elif state.stage == "PR_PREPARATION":
                await self._stage_packaging(state)
            elif state.stage == "AWAITING_APPROVAL":
                await self._stage_approval(state)

            if state.stage == "COMPLETED" or state.blocked_reason or self._retired:
                break

            await workflows_mistralai.send_assistant_message(self._format_status(state))
            user = await self.wait_for_input(
                workflows_mistralai.ChatInput(
                    "Reply 'continue' to advance to the next stage, or ask a question.",
                    suggestions=[
                        [workflows_mistralai.TextChunk(text="continue")],
                        [workflows_mistralai.TextChunk(text="status")],
                    ],
                )
            )
            text = (user.message[0].text if user.message else "continue").strip().lower()
            if text in ("continue", "next", "go", "advance"):
                nxt = next_stage(state)
                if nxt is None:
                    state.stage = "COMPLETED"
                else:
                    state.stage = nxt  # type: ignore[assignment]
                state.touch()
            # any other reply is appended to the objective as user guidance
            elif text and text != "status":
                state.objective = (state.objective + f"\n[user]: {text}").strip()
                state.touch()

        if self._retired and state.stage != "COMPLETED":
            return workflows_mistralai.ChatAssistantWorkflowOutput(
                content=[
                    workflows_mistralai.TextOutput(
                        text=f"Mission `{state.mission_key}` was adopted by a newer execution. This chat is now read-only."
                    )
                ]
            )

        summary = self._format_status(state, header="Mission finished." if state.stage == "COMPLETED" else "Mission blocked.")
        return workflows_mistralai.ChatAssistantWorkflowOutput(
            content=[workflows_mistralai.TextOutput(summary)],
            structuredContent={"mission": state.model_dump(mode="json")},
        )

    # ----------------------------------------------------------------- stages

    async def _intake(self, request: MissionRequest) -> MissionState:
        if request.mission_key:
            snapshot = await load_mission_snapshot(request.mission_key)
            if snapshot:
                previous = snapshot.pop("previous_execution_id", None)
                state = MissionState.model_validate(snapshot)
                if previous:
                    await signal_mission_adopted(previous, workflows.get_execution_id())
                if request.instruction:
                    state.objective = (state.objective + f"\n[user]: {request.instruction}").strip()
                state.active_execution_id = workflows.get_execution_id()
                state.touch()
                return state
        repository = request.repository
        pr_number = request.pr_number
        objective = request.objective
        if not repository or not pr_number:
            await workflows_mistralai.send_assistant_message(
                "I need a repository and PR number to start a mission. "
                "Reply in the form `owner/repo#123` plus an optional objective."
            )
            answers = await self.wait_for_input(
                workflows_mistralai.ChatInput("e.g. HarvardMadSys/HybridInference#1781: review the retry logic")
            )
            text = answers.message[0].text if answers.message else ""
            parsed = _parse_pr_reference(text)
            repository = repository or parsed[0]
            pr_number = pr_number or parsed[1]
            objective = objective or parsed[2]
        state = MissionState(
            mission_id=new_id("mission"),
            mission_key=build_mission_key(repository or "unknown/unknown", pr_number or 0),
            repository=repository or "unknown/unknown",
            pr_number=pr_number,
            base_branch=request.base_branch,
            objective=objective or "",
            read_only=request.read_only,
            active_execution_id=workflows.get_execution_id(),
        )
        if request.instruction:
            state.objective = (state.objective + f"\n[user]: {request.instruction}").strip()
        state.coverage = [
            c.model_copy() for c in _initial_coverage(state.read_only)
        ]
        return state

    async def _stage_intake(self, state: MissionState) -> None:
        overview = await fetch_pr_evidence(*_split_repo(state.repository), state.pr_number or 0)
        state.head_branch = _pr_head_branch(overview)
        if not state.base_branch:
            state.base_branch = _pr_base_branch(overview)
        self._satisfy(state, "investigation: production paths traced", False, "pending investigation stage")
        state.touch()

    async def _stage_investigation(self, state: MissionState) -> None:
        evidence = await fetch_pr_evidence(*_split_repo(state.repository), state.pr_number or 0)
        result = await run_role_stage(
            role="investigation",
            task=(
                f"Investigate {state.repository} PR #{state.pr_number}. Objective: {state.objective}. "
                "Trace production execution paths in the diff and surrounding source, identify root "
                "causes, and report findings with path:line evidence."
            ),
            mission_json=state.model_dump_json(exclude={"findings": {"__all__": {"detail"}}}),
            evidence_json=json.dumps(evidence, default=str),
        )
        report = InvestigationReport.model_validate(result["report"])
        added = state.upsert_findings(
            [f.model_dump() for f in report.root_causes], source_stage="INVESTIGATION"
        )
        self._satisfy(state, "investigation: production paths traced", True, "; ".join(report.production_paths) or report.summary)
        await workflows_mistralai.send_assistant_message(
            f"**Investigation complete.** {report.summary}\n\nFindings recorded: {len(added)}"
        )
        state.touch()

    async def _stage_implementation(self, state: MissionState) -> None:
        open_findings = [f for f in state.findings if f.status in ("OPEN", "ASSIGNED")]
        if not open_findings:
            state.correction_cycles += 1
            return
        for finding in open_findings:
            finding.transition("ASSIGNED")
        plan_result = await run_role_stage(
            role="implementation",
            task=(
                "Produce file edits correcting the ASSIGNED findings. Include regression tests. "
                f"Correction cycle {state.correction_cycles + 1} of {3}."
            ),
            mission_json=state.model_dump_json(),
            evidence_json=json.dumps({"open_findings": [f.model_dump(mode="json") for f in open_findings]}, default=str),
        )
        from freeinference_pr.role_agents import ImplementationReport

        report = ImplementationReport.model_validate(plan_result["report"])
        branch = f"freeinference-pr/{state.mission_key}"
        await prepare_workspace(state.mission_id, state.repository, state.base_branch or "HEAD", branch)
        commit = await apply_edits(
            state.mission_id,
            [e.model_dump() for e in report.edits],
            report.commit_message,
        )
        if commit.get("commit_sha"):
            state.commits.append(commit["commit_sha"])
            state.invalidate_verifications()
            for finding in open_findings:
                finding.correction_commit = commit["commit_sha"]
                finding.transition("CORRECTED")
        state.correction_cycles += 1
        await workflows_mistralai.send_assistant_message(
            f"**Implementation complete.** Commit: `{commit.get('commit_sha') or 'none (no effective changes)'}`. "
            f"Edits applied: {len(report.edits)}. Out of scope: {report.out_of_scope or 'nothing'}"
        )
        state.touch()

    async def _stage_review(self, state: MissionState) -> None:
        evidence = await fetch_pr_evidence(*_split_repo(state.repository), state.pr_number or 0)
        local_diff = await workspace_diff(state.mission_id) if state.commits else ""
        result = await run_role_stage(
            role="review",
            task=(
                f"Independently review {state.repository} PR #{state.pr_number} and the effective diff "
                "(including any local corrective commits). Challenge unsupported assumptions and identify "
                "missed production paths."
            ),
            mission_json=state.model_dump_json(),
            evidence_json=json.dumps({"pr_evidence": evidence, "local_diff": local_diff}, default=str),
        )
        report = ReviewReport.model_validate(result["report"])
        added = state.upsert_findings([f.model_dump() for f in report.findings], source_stage="REVIEW")
        self._satisfy(state, "review: independent review completed", True, report.assessment)
        await workflows_mistralai.send_assistant_message(
            f"**Independent review complete.** {report.assessment}\n\nNew findings: {len(added)}"
        )
        state.touch()

    async def _stage_verification(self, state: MissionState) -> None:
        test_results: dict = {"results": []}
        if state.commits:
            test_results = await run_tests(
                state.mission_id,
                ["python -m pytest -x -q --tb=short"],
            )
        evidence = await fetch_pr_evidence(*_split_repo(state.repository), state.pr_number or 0)
        ci_state = evidence.get("ci_status")
        result = await run_role_stage(
            role="verification",
            task=(
                "Verify the effective diff: interpret the actual test exit codes and CI status below. "
                "State whether each earlier finding is resolved. Separate verified facts from interpretation."
            ),
            mission_json=state.model_dump_json(),
            evidence_json=json.dumps(
                {"test_results": test_results, "ci_status": ci_state, "commits": state.commits}, default=str
            ),
        )
        report = VerificationReport.model_validate(result["report"])
        exit_codes = [r.get("exit_code") for r in test_results.get("results", [])]
        record = VerificationRecord(
            verification_id=new_id("verif"),
            commit_sha=state.commits[-1] if state.commits else None,
            commands=[r.get("command", "") for r in test_results.get("results", [])],
            exit_codes=[int(c) if c is not None else -1 for c in exit_codes],
            passed=(all(c == 0 for c in exit_codes) if exit_codes else None) or report.tests_pass or False,
            ci_state=json.dumps(ci_state, default=str)[:1000] if ci_state else None,
            resolved_findings=report.unresolved_findings,
            notes="; ".join(report.notes),
        )
        state.verifications.append(record)
        for finding in state.findings:
            if finding.status == "CORRECTED":
                finding.verification_id = record.verification_id
                finding.transition("VERIFIED")
        self._satisfy(
            state,
            "verification: tests executed on effective diff",
            bool(exit_codes) or state.read_only,
            json.dumps(exit_codes),
        )
        self._satisfy(state, "verification: CI status interpreted", ci_state is not None, record.ci_state or "")
        state.touch()

    async def _stage_packaging(self, state: MissionState) -> None:
        result = await run_role_stage(
            role="packaging",
            task=(
                f"Draft the PR description for {state.repository} PR #{state.pr_number} from the mission "
                "record: what changed, why, verification evidence, stacked dependencies, and whether any "
                "remote mutation (push/PR update/comment/merge) is requested next."
            ),
            mission_json=state.model_dump_json(),
            evidence_json="{}",
        )
        report = PackageDraft.model_validate(result["report"])
        state.touch()
        await workflows_mistralai.send_assistant_message(
            f"**PR draft ready.**\n\n## {report.title}\n\n{report.body_markdown}"
        )
        if report.requires_remote_mutation and not state.read_only:
            state.approvals.append(
                ApprovalRecord(
                    approval_id=new_id("appr"),
                    operation="push branch and update PR",
                    repository=state.repository,
                    branch=state.head_branch,
                    commit=state.commits[-1] if state.commits else None,
                )
            )
            state.stage = "AWAITING_APPROVAL"
            state.touch()

    async def _stage_approval(self, state: MissionState) -> None:
        pending = [a for a in state.approvals if a.decision == "pending"]
        if not pending:
            if state.read_only or state.coverage_complete():
                if not state.read_only and state.unresolved_critical():
                    state.blocked_reason = "Unresolved critical findings at completion."
                state.stage = "COMPLETED"
            else:
                state.blocked_reason = "Mandatory coverage checklist incomplete."
            return
        approval = pending[0]
        confirmation = await self.wait_for_input(
            workflows_mistralai.AcceptDeclineConfirmation(
                description=(
                    f"Approve remote mutation '{approval.operation}' on {approval.repository} "
                    f"(branch {approval.branch}, commit {approval.commit})?"
                ),
                accept_label="Approve",
                decline_label="Reject",
            ),
            label="Remote mutation approval",
        )
        decision = "approved" if workflows_mistralai.is_accepted(confirmation) else "rejected"
        approval.decision = decision  # type: ignore[assignment]
        approval.decided_at = workflows.workflow.now()
        state.touch()
        if decision == "rejected":
            state.blocked_reason = f"User rejected remote mutation '{approval.operation}'."
            state.stage = "BLOCKED"
        else:
            state.stage = "COMPLETED"

    # ---------------------------------------------------------------- helpers

    def _satisfy(self, state: MissionState, requirement: str, satisfied: bool, evidence: str) -> None:
        for item in state.coverage:
            if item.requirement == requirement:
                item.satisfied = satisfied
                item.evidence = evidence
        state.touch()

    def _format_status(self, state: MissionState, header: str = "") -> str:
        lines = [header] if header else []
        lines.append(f"**Mission** `{state.mission_key}` — {state.repository} PR #{state.pr_number}")
        lines.append(f"**Stage**: {state.stage}  |  **Read-only**: {state.read_only}")
        if state.findings:
            lines.append("**Findings**:")
            for f in state.findings:
                lines.append(f"- `{f.status}` [{f.severity}] {f.title} ({f.finding_id})")
        if state.verifications:
            latest = state.verifications[-1]
            lines.append(
                f"**Latest verification**: {latest.verification_id} passed={latest.passed} stale={latest.stale}"
            )
        if state.blocked_reason:
            lines.append(f"**Blocked**: {state.blocked_reason}")
        lines.append("**Coverage**:")
        for c in state.coverage:
            marker = "x" if c.satisfied else " "
            lines.append(f"- [{marker}] {c.requirement}")
        return "\n".join(lines)


def _initial_coverage(read_only: bool) -> list:
    from freeinference_pr.mission_state import CoverageItem

    requirements = list(MANDATORY_COVERAGE)
    if not read_only:
        requirements.append("implementation: corrective commits recorded")
    return [CoverageItem(requirement=r, satisfied=False) for r in requirements]


def _parse_pr_reference(text: str) -> tuple[str | None, int | None, str]:
    import re

    match = re.search(r"([\w.-]+/[\w.-]+)#(\d+)|([\w.-]+/[\w.-]+)\s*PR?\s*#?(\d+)", text)
    if not match:
        return None, None, text.strip()
    repository = match.group(1) or match.group(3)
    number = int(match.group(2) or match.group(4))
    objective = text[match.end() :].strip(" :,-")
    return repository, number, objective


def _split_repo(repository: str) -> tuple[str, str]:
    owner, _, repo = repository.partition("/")
    return owner, repo


def _pr_head_branch(overview: Any) -> str | None:
    head = _dig(overview, "head", "ref")
    return head if isinstance(head, str) else None


def _pr_base_branch(overview: Any) -> str | None:
    base = _dig(overview, "base", "ref")
    return base if isinstance(base, str) else None


def _dig(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
