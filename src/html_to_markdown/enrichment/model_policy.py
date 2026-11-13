"""Versioned routine review models and explicit, evidence-bound escalation."""

import hashlib
import json
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

ROUTINE_MODEL = "gpt-6.1-sol"
ASTRA_MODEL = "gpt-6-astra"
POLICY_VERSION = "2"
POLICY_KEY = "content_review_model_policy"
ROLE_KEY = "content_review_role"
ANNOTATION = "\nContent review model policy 2; role: "


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def apply_policy(body: dict[str, Any], role: str) -> dict[str, Any]:
    if role not in {"editor", "validator"} or body["model"] != ROUTINE_MODEL:
        raise ValueError("routine review requires Sol High; Astra needs a verified exception")
    prompt, marker, version = body["instructions"].rpartition("\nPrompt version: ")
    if not marker:
        raise ValueError("review request lacks prompt version")
    body["instructions"] = prompt + ANNOTATION + role + marker + version
    body["metadata"] = {**body.get("metadata", {}), POLICY_KEY: POLICY_VERSION, ROLE_KEY: role}
    return body


def _exception(request: dict[str, Any]) -> dict[str, Any] | None:
    if "content_review_exception" not in request.get("metadata", {}):
        return None
    data = json.loads(request["input"][0]["content"][0]["text"])
    value = data["independent_review_exception"]
    if not isinstance(value, dict):
        raise ValueError("invalid review exception evidence")
    return value


def _failed_validation(evidence: dict[str, Any], routine: dict[str, Any]) -> None:
    if (
        evidence.get("error") is not None
        or evidence.get("model") != ROUTINE_MODEL
        or evidence.get("reasoning") != "high"
        or evidence.get("transport") != "codex_litellm"
        or evidence.get("request_sha256")
        != hashlib.sha256(
            json.dumps(routine, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        or evidence.get("request") != routine
    ):
        raise ValueError("Astra escalation requires an exact completed Sol validation")
    response = evidence["response"]
    if (
        response.get("status") != "completed"
        or response.get("model") != ROUTINE_MODEL
        or _hash(response) != evidence["response_sha256"]
    ):
        raise ValueError("Astra escalation has stale or incomplete Sol response")
    content = [part for item in response.get("output", []) for part in item.get("content", [])]
    if any(part.get("type") == "refusal" for part in content):
        raise ValueError("model refusal is not an escalation prerequisite")
    texts = [part["text"] for part in content if part.get("type") == "output_text"]
    if len(texts) != 1:
        raise ValueError("escalation requires a complete structured validation")
    result = json.loads(texts[0])
    Draft202012Validator(routine["text"]["format"]["schema"]).validate(result)
    if result.get("passed") is True or (
        not result.get("failures") and not result.get("unresolved")
    ):
        raise ValueError(
            "Astra escalation needs specific unresolved preservation or privacy findings"
        )
    if any(
        event.get("type") in {"context.compacted", "thread.compacted", "turn.failed"}
        or event.get("item", {}).get("type")
        in {"command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call"}
        for event in response.get("events", [])
    ):
        raise ValueError("failed context or tool use cannot authorize escalation")


def escalate_validation(
    routine: dict[str, Any], reason: str, prior: dict[str, Any]
) -> dict[str, Any]:
    if (
        routine.get("metadata", {}).get(ROLE_KEY) != "validator"
        or routine["model"] != ROUTINE_MODEL
        or not reason.strip()
    ):
        raise ValueError("Astra escalation requires a routine validator and explicit reason")
    _failed_validation(prior, routine)
    body = deepcopy(routine)
    body["model"] = ASTRA_MODEL
    details = {"reason": reason, "prior_sol_validation": prior}
    body["metadata"]["content_review_exception"] = _hash(details)
    payload = json.loads(body["input"][0]["content"][0]["text"])
    payload["independent_review_exception"] = details
    body["input"][0]["content"][0]["text"] = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return body


def compatible_request(expected: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    """Rebuild expected requests under their original policy; never modify evidence."""
    body = deepcopy(expected)
    role = body.get("metadata", {}).get(ROLE_KEY)
    version = recorded.get("metadata", {}).get(POLICY_KEY)
    if version is None:
        body["instructions"] = body["instructions"].replace(ANNOTATION + str(role), "", 1)
        body["metadata"].pop(POLICY_KEY, None)
        body["metadata"].pop(ROLE_KEY, None)
        if not body["metadata"]:
            body.pop("metadata")
        body["model"] = ASTRA_MODEL if role == "validator" else ROUTINE_MODEL
    elif version != POLICY_VERSION or recorded["metadata"].get(ROLE_KEY) != role:
        raise ValueError("unknown or wrong review model policy/role")
    else:
        exception = _exception(recorded)
        if exception:
            if recorded["metadata"]["content_review_exception"] != _hash(exception):
                raise ValueError("changed review escalation reason or evidence")
            body = escalate_validation(body, exception["reason"], exception["prior_sol_validation"])
    if body["model"] != recorded["model"]:
        raise ValueError("wrong model for the pinned review policy")
    return body


def validation_model(request: dict[str, Any]) -> str:
    if POLICY_KEY not in request.get("metadata", {}):
        if request["model"] != ASTRA_MODEL:
            raise ValueError("historical validation requires its original Astra model")
        return ASTRA_MODEL
    if (
        request["metadata"].get(POLICY_KEY) != POLICY_VERSION
        or request["metadata"].get(ROLE_KEY) != "validator"
    ):
        raise ValueError("independent validation has wrong model policy or role")
    exception = _exception(request)
    if not exception:
        if request["model"] != ROUTINE_MODEL:
            raise ValueError(
                "routine independent review requires Sol High; Astra needs a verified exception"
            )
        return ROUTINE_MODEL
    routine = deepcopy(request)
    routine["model"] = ROUTINE_MODEL
    routine["metadata"].pop("content_review_exception")
    payload = json.loads(routine["input"][0]["content"][0]["text"])
    payload.pop("independent_review_exception")
    routine["input"][0]["content"][0]["text"] = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    )
    expected = escalate_validation(routine, exception["reason"], exception["prior_sol_validation"])
    if request != expected:
        raise ValueError("changed Astra escalation request")
    return ASTRA_MODEL


def validate_new_request(request: dict[str, Any]) -> None:
    """Completed legacy evidence may replay; new Astra spend needs a proved exception."""
    if request["model"] == ROUTINE_MODEL:
        return
    if (
        request["model"] != ASTRA_MODEL
        or request.get("metadata", {}).get(POLICY_KEY) != POLICY_VERSION
    ):
        raise ValueError(
            "new inference defaults to Sol High; Astra needs an explicit validated exception"
        )
    if validation_model(request) != ASTRA_MODEL:
        raise ValueError("new Astra inference lacks a validated exception")
