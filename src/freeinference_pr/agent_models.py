"""Structured agent output models for the independent runtime."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FindingDraft(BaseModel):
    title: str
    severity: str = Field(default="medium", description="critical|high|medium|low|info")
    evidence: list[str] = Field(default_factory=list, description="path:line or diff references")
    detail: str = ""


class ImplementationUnit(BaseModel):
    title: str
    guidance: str = ""
    paths: list[str] = Field(default_factory=list)
    security_sensitive: bool = False


class AnalysisReport(BaseModel):
    summary: str
    affected_components: list[str] = Field(default_factory=list)
    production_paths: list[str] = Field(default_factory=list)
    implementation_units: list[ImplementationUnit] = Field(default_factory=list)
    findings: list[FindingDraft] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class FileEdit(BaseModel):
    path: str
    content: str
    action: str = Field(default="write", description="write|delete")


class EditsReport(BaseModel):
    edits: list[FileEdit] = Field(default_factory=list)
    commit_message: str
    regression_tests: list[str] = Field(default_factory=list)
    notes: str = ""


class TestPlan(BaseModel):
    commands: list[str] = Field(default_factory=list)
    rationale: str = ""


class ReviewReport(BaseModel):
    assessment: str
    findings: list[FindingDraft] = Field(default_factory=list)
    missed_paths: list[str] = Field(default_factory=list)
    unsupported_assumptions: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    interpretation: str
    passed: bool | None = None
    unresolved: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PackageReport(BaseModel):
    title: str
    body_markdown: str
    publish: bool = False
    notes: str = ""
