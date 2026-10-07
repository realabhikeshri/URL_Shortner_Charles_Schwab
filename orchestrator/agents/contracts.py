"""Typed output contracts for each agent's LLM call (validated with Pydantic on every response)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from orchestrator.models import RiskLevel


class ImpactNarrative(BaseModel):
    summary: str
    risk: RiskLevel
    recommendations: list[str] = Field(default_factory=list)


class DesignDecision(BaseModel):
    decision: str
    rationale: str


class DesignDoc(BaseModel):
    title: str
    summary: str
    components: list[str]
    decisions: list[DesignDecision]
    data_model: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class DocDraft(BaseModel):
    readme: str = Field(description="Full README.md content for the service")
    changelog_added: list[str] = Field(default_factory=list)
    changelog_changed: list[str] = Field(default_factory=list)
    changelog_fixed: list[str] = Field(default_factory=list)
    changelog_security: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    verdict: Literal["approve", "request_changes"]
    summary: str
    findings: list[str] = Field(default_factory=list, description="Non-blocking observations")
    blocking: list[str] = Field(default_factory=list, description="Issues that must be fixed")


class ReleaseNotes(BaseModel):
    version: str
    highlights: list[str]
    notes: str
