"""Admission guards preserve meaning and reject stale or waived privacy decisions."""

import io

import pytest
from PIL import Image

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_review import (
    AdmissionValidation,
    FindingDecision,
    Label,
    MediaChange,
    PrivacyDecision,
    SyntheticValue,
    TextChange,
    apply_text,
    enforce,
    preview_media,
    scan,
    validate_admission,
)


def proposal(**updates: object) -> PrivacyDecision:
    return PrivacyDecision.model_validate(
        {
            "identity": "doc",
            "binding_sha256": "a" * 64,
            "findings": [],
            "values": [],
            "text_changes": [],
            "media_changes": [],
            "unresolved": [],
            **updates,
        }
    )


def validation(**updates: object) -> AdmissionValidation:
    return AdmissionValidation.model_validate(
        {
            "identity": "doc",
            "binding_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "approved": True,
            "relevance_valid": True,
            "privacy_valid": True,
            "extraction_valid": True,
            "technical_relationships_preserved": True,
            "media_valid": True,
            "retirement_valid": True,
            "complete_document_reviewed": True,
            "complete_media_reviewed": True,
            "approved_safe_findings": [],
            "failures": [],
            "unresolved": [],
            **updates,
        }
    )


def test_exact_identifying_replacement_preserves_payload_and_metadata() -> None:
    original = "Connect to robin@customer.invalid, then run `curl --retry 3` within 30 seconds."
    value = "robin@customer.invalid"
    start = original.index(value)
    decision = proposal(
        values=[SyntheticValue(source_sha256=sha(value), replacement="user@example.com")],
        text_changes=[
            TextChange(
                field="body",
                start=start,
                end=start + len(value),
                source_sha256=sha(value),
                replacement="user@example.com",
                reason="identifying contact",
            )
        ],
    )
    meta = {"title": "A service", "url": "https://public.example.com/article", "tags": ["cloud"]}
    result_meta, result = apply_text(meta, original, decision)
    assert result_meta == meta
    assert "user@example.com" in result and "`curl --retry 3` within 30 seconds" in result
    with pytest.raises(ValueError, match="digest"):
        apply_text(meta, original.replace(value, "other@customer.invalid"), decision)
    with pytest.raises(ValueError, match="inconsistent"):
        apply_text(meta, original, decision.model_copy(update={"values": []}))


def test_mapping_feedback_reports_every_exact_guard_without_source_values() -> None:
    original = "private-one private-two"
    changes = []
    for value in original.split():
        start = original.index(value)
        changes.append(
            TextChange(
                field="body",
                start=start,
                end=start + len(value),
                source_sha256=sha(value),
                replacement="example-value",
                reason="identifying value",
            )
        )
    with pytest.raises(ValueError, match="inconsistent") as failure:
        apply_text({}, original, proposal(text_changes=changes))
    message = str(failure.value)
    for change in changes:
        assert change.source_sha256 in message
        assert f"body:{change.start}:{change.end}" in message
    assert "private-one" not in message and "private-two" not in message
    corrected = proposal(
        text_changes=changes,
        values=[
            SyntheticValue(source_sha256=change.source_sha256, replacement=change.replacement)
            for change in changes
        ],
    )
    assert apply_text({}, original, corrected)[1] == "example-value example-value"


def test_documented_examples_need_independent_finding_specific_approval() -> None:
    findings = scan({"body": "The documentation uses 192.0.2.1 and author@example.com."})
    decisions = [
        FindingDecision(
            finding_id=item["id"],
            disposition="documentation_example",
            reason="reserved documentation example",
        )
        for item in findings
    ]
    current = proposal(findings=decisions)
    enforce(findings, current, validation(approved_safe_findings=[item["id"] for item in findings]))
    with pytest.raises(ValueError, match="unresolved"):
        enforce(findings, current, validation())
    with pytest.raises(ValueError, match="stale"):
        enforce(findings, current, validation(approved_safe_findings=["stale"]))


def test_credentials_cannot_be_waived_by_both_models() -> None:
    findings = scan({"body": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"})
    current = proposal(
        findings=[
            FindingDecision(
                finding_id=item["id"],
                disposition="documentation_example",
                reason="incorrect example approval",
            )
            for item in findings
        ]
    )
    with pytest.raises(ValueError, match="credential"):
        enforce(
            findings, current, validation(approved_safe_findings=[item["id"] for item in findings])
        )


@pytest.mark.parametrize(
    "field", ["metadata/url", "metadata/publication_date", "metadata/canonical_url"]
)
def test_privacy_cannot_change_provenance_or_source_identity(field: str) -> None:
    current = proposal(
        text_changes=[
            TextChange(
                field=field,
                start=0,
                end=1,
                source_sha256=sha("x"),
                replacement="y",
                reason="bad identity edit",
            )
        ]
    )
    with pytest.raises(ValueError, match="identity"):
        apply_text({}, "", current)


def test_image_replacement_is_exact_and_consistent_with_text() -> None:
    image = Image.new("RGB", (160, 80), "blue")
    output = io.BytesIO()
    image.save(output, format="PNG")
    raw = output.getvalue()
    label = Label(
        rectangle=[10, 10, 140, 40],
        source_value_sha256=sha("identifying tenant"),
        text="example-tenant",
        size=10,
    )
    change = MediaChange(
        source_sha256=sha(raw),
        rectangles=[],
        labels=[label],
        frames=[],
        strip_metadata=True,
        reason="identifying tenant label",
    )
    result, policy = preview_media(raw, change, {label.source_value_sha256: label.text})
    assert policy["output_sha256"] == sha(result)
    with Image.open(io.BytesIO(result)) as changed:
        assert changed.getpixel((159, 79)) == (0, 0, 255)
    assert preview_media(raw, change, {label.source_value_sha256: label.text})[0] == result
    with pytest.raises(ValueError, match="disagree"):
        preview_media(raw, change, {})


def test_independent_complete_preservation_is_mandatory() -> None:
    validate_admission("doc", "a" * 64, "b" * 64, validation())
    for field in ("complete_media_reviewed", "technical_relationships_preserved", "privacy_valid"):
        with pytest.raises(ValueError, match="failed"):
            validate_admission("doc", "a" * 64, "b" * 64, validation(**{field: False}))
    with pytest.raises(ValueError, match="stale"):
        validate_admission("other", "a" * 64, "b" * 64, validation())


@pytest.mark.parametrize("prefix", ["token=", '"token": "', "Authorization: Bearer "])
@pytest.mark.parametrize("value", ["EXAMPLE_API_TOKENextra123456", "real123456EXAMPLE_API_TOKEN"])
def test_placeholder_prefix_or_suffix_cannot_hide_credential(prefix: str, value: str) -> None:
    assert any(
        finding["kind"] in {"credential", "authorization-header"}
        for finding in scan({"body": prefix + value})
    )


@pytest.mark.parametrize(
    "text",
    [
        "token=EXAMPLE_API_TOKEN",
        'token="EXAMPLE_API_TOKEN"',
        "Authorization: Bearer EXAMPLE_API_TOKEN",
    ],
)
def test_only_exact_documentation_placeholder_is_exempt(text: str) -> None:
    assert not scan({"body": text})


def test_quoted_documentation_placeholder_is_exact_and_still_exempt() -> None:
    assert not scan({"body": '{"api_key": "EXAMPLE_API_TOKEN"}'})
