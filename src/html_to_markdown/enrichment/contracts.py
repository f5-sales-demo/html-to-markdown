"""Strict, versioned model and replay contracts."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "article-enrichment/1"
EDITOR_MODEL = "gpt-6.1-sol"
VALIDATOR_MODEL = "gpt-6-astra"
REASONING = "high"

Classification = Literal[
    "article_explanation",
    "prerequisite",
    "action",
    "constraint",
    "warning",
    "example",
    "reference",
    "useful_media",
    "generic_setup",
    "promotion",
    "navigation",
    "duplicate",
    "extraction_defect",
    "shell",
    "uncertain",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClassificationDecision(StrictModel):
    address: str
    classification: Classification
    evidence: str
    uncertain: bool


class Edit(StrictModel):
    address: str
    source_sha256: str
    source: str
    replacement: str
    count: int
    reason: str
    evidence: list[str]
    repair_evidence: str | None


class ProtectedFact(StrictModel):
    address: str
    source_quote: str
    retained_quote: str
    kind: Literal["fact", "prerequisite", "warning", "condition", "action", "payload"]


class MediaDecision(StrictModel):
    reference: str
    sha256: str
    disposition: Literal["keep", "remove", "uncertain"]
    alt: str
    evidence: str


class Decision(StrictModel):
    document: str
    input_sha256: str
    disposition: Literal["rewrite", "retain", "exclude_shell", "alias"]
    canonical_document: str | None
    reason: str
    description: str
    description_evidence: list[str]
    classifications: list[ClassificationDecision]
    edits: list[Edit]
    protected_facts: list[ProtectedFact]
    retained_prerequisites: list[str]
    media: list[MediaDecision]
    unresolved: list[str]


class FactCheck(StrictModel):
    source_quote: str
    retained_quote: str
    preserved: bool
    kind: Literal["fact", "prerequisite", "warning", "condition", "action", "payload"]


class Validation(StrictModel):
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
    protected_facts: list[FactCheck]
    unsupported_claims: list[str]
    failures: list[str]
    unresolved: list[str]


class ImageAnalysis(StrictModel):
    sha256: str
    description: str
    visible_text: list[str]
    technical_facts: list[str]
    uncertain: bool


class ImageCollection(StrictModel):
    images: list[ImageAnalysis]


class ResponseEvidence(StrictModel):
    request_sha256: str
    response_sha256: str
    model: str
    reasoning: str
    prompt_version: str
    request: dict[str, Any]
    response: dict[str, Any]
    error: str | None
    transport: Literal["openai_batch", "codex_litellm"] = "openai_batch"
    runtime_version: str | None = None
    output_selector: str | None = None


class DocumentEvidence(StrictModel):
    document: str
    source: str
    input_sha256: str
    editor: ResponseEvidence | None
    validator: ResponseEvidence | None


class Artifact(StrictModel):
    schema_version: Literal[1]
    prompt_version: str
    baseline_tag: str
    publication_sha256: str
    baseline_manifest_sha256: str
    curated_sha256: str
    analysis_sha256: str
    policy_sha256: str
    api_policy_sha256: str
    documents: list[DocumentEvidence]
    assets: dict[str, str]
    images: dict[str, ResponseEvidence]
    image_groups: dict[str, str] = Field(default_factory=dict)
    unresolved_media: dict[str, str] = Field(default_factory=dict)
    repairs: dict[str, dict[str, Any]]
    pricing: dict[str, dict[str, float]]
    # Rates are USD per million tokens. Empty pricing is explicitly unpriced,
    # never a claim of zero spending.
