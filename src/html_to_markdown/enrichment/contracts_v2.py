"""Structural references replace model-reproduced source strings in version two."""

from typing import Literal

from ..source_review import FindingDecision
from .contracts import Classification, MediaDecision, StrictModel

VERSION = "article-enrichment/2"


class ClassificationV2(StrictModel):
    address: str
    source_sha256: str
    classification: Classification
    evidence_addresses: list[str]
    reason: str
    uncertain: bool


class ClassifiedSection(StrictModel):
    document: str
    input_sha256: str
    section: str
    classifications: list[ClassificationV2]


class EditV2(StrictModel):
    address: str
    source_sha256: str
    replacement: str
    reason: str
    evidence_addresses: list[str]
    repair_evidence: str | None


class EditedSection(StrictModel):
    document: str
    input_sha256: str
    section: str
    edits: list[EditV2]
    reason: str
    unresolved: list[str]


class DocumentDecisionV2(StrictModel):
    document: str
    input_sha256: str
    disposition: Literal["rewrite", "retain", "exclude_shell", "alias"]
    canonical_document: str | None
    description: str
    description_evidence: list[str]
    reason: str
    media: list[MediaDecision]
    privacy_findings: list[FindingDecision]
    unresolved: list[str]


class FactV2(StrictModel):
    source_address: str
    source_sha256: str
    candidate_addresses: list[str]
    candidate_sha256: list[str]
    necessary: bool
    preserved: bool
    kind: Literal["fact", "prerequisite", "warning", "condition", "action", "payload"]


class ValidationV2(StrictModel):
    document: str
    input_sha256: str
    candidate_sha256: str
    passed: bool
    disposition_valid: bool
    description_grounded: bool
    coherent_standalone: bool
    action_order_preserved: bool
    all_necessary_facts_preserved: bool
    media_valid: bool
    classifications_complete: bool
    privacy_valid: bool
    approved_safe_privacy_findings: list[str]
    protected_facts: list[FactV2]
    unsupported_claims: list[str]
    failures: list[str]
    unresolved: list[str]
