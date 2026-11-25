"""All new inference stays Sol High; historical decisions remain read only."""

import json
from copy import deepcopy

import pytest

from html_to_markdown.enrichment.contracts import StrictModel
from html_to_markdown.enrichment.model_policy import (
    POLICY_KEY,
    compatible_request,
    validate_new_request,
    validation_model,
)
from html_to_markdown.enrichment.requests import request


class Validation(StrictModel):
    passed: bool


def body():
    return request(
        "gpt-6.1-sol",
        "Independent review",
        {"source": "complete"},
        Validation,
        role="validator",
    )


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.2-sol", "unknown"])
def test_every_other_model_fails_before_new_inference(model):
    value = body()
    value["model"] = model
    with pytest.raises(ValueError, match="Sol High"):
        validate_new_request(value)


@pytest.mark.parametrize("reasoning", [None, {}, {"effort": "low"}, {"effort": "medium"}])
def test_non_high_reasoning_fails_before_new_inference(reasoning):
    value = body()
    value["reasoning"] = reasoning
    with pytest.raises(ValueError, match="Sol High"):
        validate_new_request(value)


def test_current_policy_is_version_three_and_accepts_sol_high():
    value = body()
    assert value["metadata"][POLICY_KEY] == "3"
    validate_new_request(value)
    assert validation_model(value) == "gpt-6.1-sol"


def test_version_two_sol_request_replays_exactly_without_mutation():
    current = body()
    old = deepcopy(current)
    old["metadata"][POLICY_KEY] = "2"
    old["instructions"] = old["instructions"].replace(
        "Content review model policy 3", "Content review model policy 2"
    )
    original = deepcopy(old)
    assert compatible_request(current, old) == old == original
    assert validation_model(old) == "gpt-6.1-sol"
    validate_new_request(old)


def test_current_policy_cannot_carry_a_retired_exception():
    value = body()
    value["metadata"]["content_review_exception"] = "claimed exception"
    with pytest.raises(ValueError, match="exception"):
        validate_new_request(value)


@pytest.mark.parametrize("change", ["other_model", "non_high", "retired_exception"])
def test_queue_and_native_executor_reject_before_dispatch(tmp_path, monkeypatch, change):
    from test_codex_worker import body as native_body
    from test_codex_worker import client

    from html_to_markdown.inference_queue import submit

    value = native_body()
    if change == "other_model":
        value["model"] = "gpt-6-astra"
    elif change == "non_high":
        value["reasoning"] = {"effort": "medium"}
    else:
        value["metadata"] = {"content_review_exception": "retired"}
    queue = tmp_path / "queue"
    with pytest.raises(ValueError):
        submit(queue, value, "a" * 64)
    assert not queue.exists()

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("disallowed request dispatched inference")

    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run", unexpected_dispatch
    )
    journal = tmp_path / "journal"
    journal.mkdir()
    evidence = client(journal).execute(value)
    assert evidence.error and evidence.response == {}
    attempts = list(journal.glob("*.attempt-*.json"))
    assert attempts and all(
        json.loads(path.read_bytes())["raw"]["invoked"] is False for path in attempts
    )
