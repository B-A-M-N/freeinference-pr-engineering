"""Durable mission state shared by Vibe Web and Vibe CLI.

A *mission* is one PR engineering engagement (repository + PR + objective).
The authoritative record lives in the workflow execution's durable state and is
exposed through query handlers; the CLI and the web interface both reach the
same execution through the Mistral API/MCP. ``mission_key`` is the stable
cross-interface identifier.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal[
    "INTAKE",
    "INVESTIGATION",
    "IMPLEMENTATION",
    "REVIEW",
    "VERIFICATION",
    "PR_PREPARATION",
    "AWAITING_APPROVAL",
    "COMPLETED",
    "BLOCKED",
]

STAGE_ORDER: list[str] = [
    "INTAKE",
    "INVESTIGATION",
    "IMPLEMENTATION",
    "REVIEW",
    "VERIFICATION",
    "PR_PREPARATION",
    "AWAITING_APPROVAL",
    "COMPLETED",
]

FindingStatus = Literal["OPEN", "ASSIGNED", "CORRECTED", "VERIFIED", "CLOSED"]
FINDING_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"ASSIGNED", "CLOSED"},
    "ASSIGNED": {"CORRECTED", "OPEN"},
    "CORRECTED": {"VERIFIED", "ASSIGNED"},
    "VERIFIED": {"CLOSED", "CORRECTED"},
    "CLOSED": set(),
}

Severity = Literal["critical", "high", "medium", "low", "info"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mission_key(repository: str, pr_number: int) -> str:
    owner, _, repo = repository.partition("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{owner}-{repo}-pr{pr_number}").strip("-").lower()
    return slug


def new_id(prefix: str) -> str:
    from uuid import uuid4

    return f"{prefix}_{uuid4().hex[:12]}"


class Finding(BaseModel):
    finding_id: str
    title: str
    severity: Severity = "medium"
    status: FindingStatus = "OPEN"
    evidence: list[str] = Field(default_factory=list, description="Source-backed references, e.g. path:line")
    detail: str = ""
    source_stage: Literal["INVESTIGATION", "REVIEW", "VERIFICATION"] = "INVESTIGATION"
    correction_commit: str | None = None
    verification_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def transition(self, new_status: FindingStatus) -> None:
        allowed = FINDING_TRANSITIONS[self.status]
        if new_status not in allowed:
            raise ValueError(f"Invalid finding transition {self.status} -> {new_status}")
        self.status = new_status
        self.updated_at = utcnow()


class FileEdit(BaseModel):
    path: str
    content: str
    action: Literal["write", "delete"] = "write"


class ImplementationReport(BaseModel):
    edits: list[FileEdit] = Field(default_factory=list)
    commit_message: str
    rationale: str = ""
    regression_tests: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class VerificationRecord(BaseModel):
    verification_id: str
    commit_sha: str | None = None
    commands: list[str] = Field(default_factory=list)
    exit_codes: list[int] = Field(default_factory=list)
    passed: bool = False
    ci_state: str | None = None
    resolved_findings: list[str] = Field(default_factory=list)
    stale: bool = False
    notes: str = ""
    verified_at: datetime = Field(default_factory=utcnow)


class ApprovalRecord(BaseModel):
    approval_id: str
    operation: str
    repository: str
    branch: str | None = None
    commit: str | None = None
    decision: Literal["approved", "rejected", "pending"] = "pending"
    decided_at: datetime | None = None
    requested_at: datetime = Field(default_factory=utcnow)


class CoverageItem(BaseModel):
    requirement: str
    satisfied: bool
    evidence: str = ""


class MissionState(BaseModel):
    mission_id: str
    mission_key: str
    repository: str
    pr_number: int | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    objective: str = ""
    stage: Stage = "INTAKE"
    read_only: bool = True
    active_execution_id: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    verifications: list[VerificationRecord] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    coverage: list[CoverageItem] = Field(default_factory=list)
    correction_cycles: int = 0
    blocked_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        self.updated_at = utcnow()

    def upsert_findings(self, drafts: list[dict], source_stage: str) -> list[str]:
        """Add new findings; returns the ids of the findings added."""
        added: list[str] = []
        existing_titles = {f.title.strip().lower() for f in self.findings}
        for draft in drafts:
            title = str(draft.get("title", "")).strip()
            if not title or title.lower() in existing_titles:
                continue
            finding = Finding(
                finding_id=new_id("find"),
                title=title,
                severity=draft.get("severity", "medium"),
                evidence=list(draft.get("evidence", [])),
                detail=draft.get("detail", ""),
                source_stage=source_stage,
            )
            self.findings.append(finding)
            existing_titles.add(title.lower())
            added.append(finding.finding_id)
        self.touch()
        return added

    def invalidate_verifications(self) -> None:
        """Any source change makes earlier verification stale."""
        for v in self.verifications:
            v.stale = True
        self.touch()

    def unresolved_critical(self) -> list[Finding]:
        return [
            f
            for f in self.findings
            if f.severity == "critical" and f.status not in ("VERIFIED", "CLOSED")
        ]

    def coverage_complete(self) -> bool:
        required = [c for c in self.coverage if c.requirement in MANDATORY_COVERAGE]
        return bool(required) and all(c.satisfied for c in required)


MANDATORY_COVERAGE = (
    "investigation: production paths traced",
    "review: independent review completed",
    "verification: tests executed on effective diff",
    "verification: CI status interpreted",
)
