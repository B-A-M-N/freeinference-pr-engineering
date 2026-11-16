"""Role agents for the PR engineering system.

Every role agent runs on FreeInference through ``FreeInferenceCompletionConfig``
(see ``models_catalog`` for per-role model assignment). Agents run inside
workflow activities with the Agents SDK's local runtime; each stage task is a
bounded, single-turn engagement whose context is rebuilt from the durable
mission record, so worker restarts never lose mission state.

Role agents are reasoners over *evidence bundles* fetched by deterministic
GitHub activities: the workflow owns all remote I/O, agents own analysis and
structured conclusions. This keeps tool-calling requirements on the provider
minimal and makes every finding reproducible from recorded evidence.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mistralai import agents

from freeinference_pr.mission_state import ImplementationReport
from freeinference_pr.models_catalog import completion_for_role


class FindingDraft(BaseModel):
    title: str
    severity: str = Field(default="medium", description="critical|high|medium|low|info")
    evidence: list[str] = Field(default_factory=list, description="path:line or URL references")
    detail: str = ""


class InvestigationReport(BaseModel):
    summary: str
    root_causes: list[FindingDraft] = Field(default_factory=list)
    affected_contracts: list[str] = Field(default_factory=list)
    production_paths: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    assessment: str = Field(description="overall assessment of the effective diff")
    findings: list[FindingDraft] = Field(default_factory=list)
    missed_production_paths: list[str] = Field(default_factory=list)
    unsupported_assumptions: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    interpretation: str
    tests_pass: bool | None = Field(
        default=None, description="model's interpretation; the workflow records actual exit codes"
    )
    unresolved_findings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PackageDraft(BaseModel):
    title: str
    body_markdown: str
    stacked_dependencies: list[str] = Field(default_factory=list)
    scope_confirmation: str = ""
    requires_remote_mutation: bool = False


ROLE_INSTRUCTIONS: dict[str, str] = {
    "orchestrator": (
        "You are the PR engineering orchestrator. Given the mission record and the "
        "conversation context, decide the next bounded task for the pipeline stage and "
        "report blockers honestly. Never claim work is complete without verification "
        "records. Never expand scope beyond the stated objective."
    ),
    "investigation": (
        "You are the investigation agent. Inspect the provided source evidence, trace "
        "production execution paths, and identify root causes. Every finding MUST cite "
        "concrete evidence (path:line, diff hunk, CI log line). Never speculate: if "
        "evidence is missing, list it under open_questions instead."
    ),
    "implementation": (
        "You are the implementation agent. Produce narrowly scoped file edits that "
        "correct the assigned findings, plus regression tests. Preserve unrelated "
        "changes. Report every edit as a full file content for path, and keep the "
        "commit message factual."
    ),
    "review": (
        "You are an independent review agent. Review the source and the effective diff "
        "for correctness, security, concurrency, and integration problems. You did not "
        "write this code: challenge unsupported assumptions, identify missed production "
        "paths, and report actionable findings with severity and evidence."
    ),
    "verification": (
        "You are the verification agent. Interpret the actual test output and CI "
        "records provided to you. Distinguish verified facts from interpretation. "
        "State explicitly whether each earlier finding is resolved by the evidence."
    ),
    "packaging": (
        "You are the PR packaging agent. Draft an accurate PR description from the "
        "mission record: what changed, why, and the verification evidence. List any "
        "stacked-PR dependencies. Flag whether the next step requires a remote "
        "mutation (push, PR create/update, comment, merge)."
    ),
}


def build_role_agent(role: str) -> agents.Agent:
    if role not in ROLE_INSTRUCTIONS:
        raise ValueError(f"Unknown role: {role}")
    completion = completion_for_role(role)
    return agents.Agent(
        model=completion.model,
        name=f"pr-{role}",
        instructions=ROLE_INSTRUCTIONS[role],
        completion=completion,
    )


OUTPUT_TYPES: dict[str, type[BaseModel]] = {
    "investigation": InvestigationReport,
    "implementation": ImplementationReport,
    "review": ReviewReport,
    "verification": VerificationReport,
    "packaging": PackageDraft,
}
