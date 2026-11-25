"""Historical controller reuse requires exact captured core and a qualified code pair."""

import pytest

from html_to_markdown import review_controller_compatibility as compatibility

OLD_CONTEXT = "a" * 64
NEW_CONTEXT = "b" * 64
CURRENT_CODE = "c" * 64


@pytest.fixture
def registry(monkeypatch):
    value = {
        "schema_version": 1,
        "controller_sha256": CURRENT_CODE,
        "previous": [
            {
                "source_commit": "d" * 40,
                "software_tag": "v1.14.33",
                "wheel_sha256": "e" * 64,
                "controller_sha256": "f" * 64,
                "request_context_sha256": OLD_CONTEXT,
            }
        ],
    }
    monkeypatch.setattr(compatibility, "read_registry", lambda: value)
    monkeypatch.setattr(compatibility, "controller_digest", lambda: CURRENT_CODE)
    return value


def binding(context):
    return {
        "identity": "my-f5-com:K000123456",
        "article_sha256": "0" * 64,
        "source_article_html_sha256": "1" * 64,
        "media": [],
        "metadata": {"title": "Public documentation"},
        "curation_policy_sha256": "2" * 64,
        "api_policy_sha256": "3" * 64,
        "model_policy_version": 2,
        "editor_model": "gpt-6.1-sol",
        "validator_model": "gpt-6.1-sol",
        "request_context_sha256": context,
    }


def test_qualified_pair_retains_the_original_binding_without_rebinding_responses(registry):
    old = binding(OLD_CONTEXT)
    record = {"binding": old, "phases": {"relevance": {"request_sha256": "4" * 64}}}
    result = compatibility.admission_binding(record, binding(NEW_CONTEXT))
    assert result == old
    assert record["phases"]["relevance"]["request_sha256"] == "4" * 64
    assert record["binding"] == old


@pytest.mark.parametrize(
    "field,value",
    [
        ("article_sha256", "5" * 64),
        ("source_article_html_sha256", "6" * 64),
        ("metadata", {"title": "Changed authored title"}),
        ("media", [{"sha256": "7" * 64}]),
        ("curation_policy_sha256", "8" * 64),
        ("api_policy_sha256", "9" * 64),
        ("model_policy_version", 3),
        ("validator_model", "gpt-6-astra"),
    ],
)
def test_changed_core_remains_a_fresh_review(registry, field, value):
    current = {**binding(NEW_CONTEXT), field: value}
    assert compatibility.admission_binding({"binding": binding(OLD_CONTEXT)}, current) == current


def test_unregistered_context_cannot_be_reused(registry):
    current = binding(NEW_CONTEXT)
    assert compatibility.admission_binding({"binding": binding("0" * 64)}, current) == current


def test_changed_unqualified_controller_cannot_reuse_old_approvals(registry):
    registry["controller_sha256"] = "0" * 64
    current = binding(NEW_CONTEXT)
    assert compatibility.admission_binding({"binding": binding(OLD_CONTEXT)}, current) == current


def test_pending_or_exhausted_ledgers_are_preserved_and_gain_no_approval(registry):
    record = {
        "binding": binding(OLD_CONTEXT),
        "privacy_exhausted": True,
        "privacy_feedback": ["first failure", "second failure"],
        "disposition": "queued",
        "phases": {},
    }
    original = dict(record)
    assert compatibility.admission_binding(record, binding(NEW_CONTEXT)) == record["binding"]
    assert record == original and record["disposition"] == "queued"


def test_registry_rejects_unpinned_or_malformed_controller_pairs(registry):
    registry["previous"][0]["wheel_sha256"] = "not a digest"
    with pytest.raises(ValueError, match="compatibility"):
        compatibility.admission_binding({"binding": binding(OLD_CONTEXT)}, binding(NEW_CONTEXT))


def sol_migration(context, version):
    return {**binding(context), "model_policy_version": version, "reasoning": "high"}


def test_registered_sol_policy_two_migration_preserves_complete_binding_and_history(registry):
    old = sol_migration(OLD_CONTEXT, 2)
    current = sol_migration(NEW_CONTEXT, 3)
    record = {
        "binding": old,
        "phases": {"privacy": {"request_sha256": "4" * 64}},
        "privacy_feedback": ["first failure", "second failure"],
        "privacy_exhausted": True,
        "disposition": "queued",
    }
    original = dict(record)
    assert compatibility.admission_binding(record, current) == old
    assert record == original
    assert record["privacy_exhausted"] and record["disposition"] == "queued"


@pytest.mark.parametrize(
    "field,value",
    [
        ("article_sha256", "5" * 64),
        ("source_article_html_sha256", "6" * 64),
        ("metadata", {"title": "Changed authored title"}),
        ("media", [{"sha256": "7" * 64}]),
        ("capture_provenance", {"http_status": 401}),
        ("curation_policy_sha256", "8" * 64),
        ("api_policy_sha256", "9" * 64),
        ("model_policy_version", 4),
        ("editor_model", "gpt-6-astra"),
        ("validator_model", "gpt-6-astra"),
        ("reasoning", "medium"),
    ],
)
def test_sol_policy_transition_never_waives_other_core_guards(registry, field, value):
    old = sol_migration(OLD_CONTEXT, 2)
    current = {**sol_migration(NEW_CONTEXT, 3), field: value}
    assert compatibility.admission_binding({"binding": old}, current) == current


@pytest.mark.parametrize(
    "change",
    [
        {"model_policy_version": 1},
        {"model_policy_version": "2"},
        {"editor_model": "gpt-6-astra"},
        {"validator_model": "gpt-6-astra"},
        {"reasoning": "medium"},
        {"request_context_sha256": "0" * 64},
    ],
)
def test_unqualified_old_model_contract_cannot_enter_sol_migration(registry, change):
    old = {**sol_migration(OLD_CONTEXT, 2), **change}
    current = sol_migration(NEW_CONTEXT, 3)
    assert compatibility.admission_binding({"binding": old}, current) == current
