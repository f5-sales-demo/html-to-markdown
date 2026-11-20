"""Private privacy prerequisites over complete verified published Markdown and media."""

from __future__ import annotations

import json
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .baseline_dispositions import review_relevance
from .baseline_late_validation import (
    _execution_digest,
    reusable_existing_validation,
    validate_existing_baseline,
)
from .baseline_review_renewal import (
    preparation_controller,
    prepare_existing_proposal,
)
from .baseline_source_context import _relevance_evidence, baseline_context
from .baseline_validation_requests import validation_request as _validation_request
from .content_inventory import atomic_private
from .curation import json_bytes, load_curation_policy, media_inventory
from .curation_model_evidence import save as _save
from .enrichment.analysis import sha
from .enrichment.clients import client_for_host
from .enrichment.codex_client import CodexClient
from .enrichment.contracts import EDITOR_MODEL, VALIDATOR_MODEL
from .enrichment.gates import request_hash
from .errors import TransportFailure
from .media_observation_jobs import confirmed_media_values, prepare_media_observations
from .refresh import _verified_baseline
from .render import split_document
from .source_boundary_review import (
    execute_cached_source as _execute,
)
from .source_boundary_review import (
    parse_source_response as parse_response,
)
from .source_phase_bindings import independently_validated_privacy, privacy_context
from .source_review import (
    AdmissionValidation,
    PrivacyDecision,
    enforce,
    review_context_digest,
)
from .source_review_jobs import _request

__all__ = [
    "baseline_context",
    "prepare_baseline_reviews",
    "review_baseline_privacy",
    "review_baseline_batch",
    "verify_baseline_prerequisite",
]

VERSION = "baseline-source-privacy/3"
LIMIT = 2


def _controller() -> str:
    return preparation_controller()


# Preserve source custody, frame enumeration and checkpoint binding in one transaction.
# pylint: disable-next=too-many-locals
def prepare_baseline_reviews(
    baseline: Path, publication_sha256: str, documents: list[str], output: Path
) -> dict[str, Any]:
    """Prepare complete archived inputs; this makes no fresh-capture or privacy claim."""
    verified = _verified_baseline(baseline, publication_sha256)
    entries = []
    for article in sorted(set(documents)):
        path = Path(article)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.parts[:1] != ("content",)
            or path.name != "index.md"
            or article not in verified
        ):
            raise ValueError("dependency lacks a verified baseline article")
        publication = json.loads((baseline / "publication.json").read_bytes())
        files = {
            "articles/" + name: data
            for name, data in verified.items()
            if name == article or Path(name).parent == path.parent / "assets"
        }
        hashes = {name: sha(data) for name, data in sorted(files.items())}
        source = output / "inputs" / publication_sha256 / sha(article)
        for name, data in files.items():
            atomic_private(source / name, data)
        metadata, body = split_document(verified[article].decode())
        inventory = media_inventory(source / "articles" / article, body)
        if any(item["kind"] != "local" or not item["sha256"] for item in inventory):
            raise ValueError("baseline dependency has unavailable or external media")
        media = []
        for item in inventory:
            relative = (Path("articles") / path.parent / item["reference"]).as_posix()
            entry = {
                "path": relative,
                "url": item["reference"],
                "sha256": item["sha256"],
                "media_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
            }
            if entry not in media:
                media.append(entry)
        binding = {
            "input_kind": "retained_verified_baseline",
            "baseline_receipt_sha256": publication_sha256,
            "baseline_article": article,
            "baseline_article_sha256": sha(verified[article]),
            "source_files_sha256": sha(json_bytes(hashes)),
            "source_privacy_controller_sha256": review_context_digest(),
            "preparation_controller_sha256": _controller(),
            "curation_policy_sha256": load_curation_policy().sha256,
            "version": VERSION,
        }
        prior_relevance = _relevance_evidence(
            output, article, sha(verified[article]), publication_sha256
        )
        if prior_relevance:
            binding["relevance_evidence_sha256"] = sha(json_bytes(prior_relevance))
        record = output / "documents" / (sha(article) + ".json")
        previous = json.loads(record.read_bytes()) if record.exists() else {}
        if reusable_existing_validation(previous, binding):
            entries.append(
                {"identity": metadata["url"], "path": record.relative_to(output).as_posix()}
            )
            continue
        if previous.get("binding") != binding:
            if previous:
                original_bytes = record.read_bytes()
                atomic_private(output / "history" / (sha(original_bytes) + ".json"), original_bytes)
            renewed = prepare_existing_proposal(output, previous, binding, _execution_digest())
            if renewed is not None:
                atomic_private(record, json_bytes(renewed))
                entries.append(
                    {"identity": metadata["url"], "path": record.relative_to(output).as_posix()}
                )
                continue
            feedback = (
                previous.get("privacy_feedback", [])
                if previous.get("binding", {}).get("source_files_sha256")
                == binding["source_files_sha256"]
                else []
            )
            if previous:
                atomic_private(
                    output / "history" / (sha(record.read_bytes()) + ".json"), record.read_bytes()
                )
            saved = {
                "binding": binding,
                "binding_sha256": sha(json_bytes(binding)),
                "identity": metadata["url"],
                "source": source.relative_to(output).as_posix(),
                "input_file_hashes": hashes,
                "source_published_at": publication["published_at"],
                "item": {
                    "identity": metadata["url"],
                    "article_path": "articles/" + article,
                    "published_path": "articles/" + article,
                    "media": media,
                },
                "phases": {},
                "privacy_feedback": feedback,
                "status": "privacy_review_required",
                "fresh_capture": False,
            }
            atomic_private(record, json_bytes(saved))
        entries.append({"identity": metadata["url"], "path": record.relative_to(output).as_posix()})
    result = {
        "version": VERSION,
        "baseline_receipt_sha256": publication_sha256,
        "documents": entries,
    }
    atomic_private(output / "inventory.json", json_bytes(result))
    return result


# Privacy proposal, independent validation and source output commit share one transaction.
# pylint: disable-next=too-many-locals,too-many-statements
def review_baseline_privacy(output: Path, record: Path, client: Any = None) -> dict[str, Any]:
    """Bound corrections and exact replay precede any retirement proposal."""
    from .source_review_jobs import _compile_admission  # pylint: disable=import-outside-toplevel

    saved: dict[str, Any] = json.loads(record.read_bytes())
    # pylint: disable-next=import-outside-toplevel
    from .baseline_global_ledgers import verify_record_ledger

    verify_record_ledger(output, saved)
    if saved.get("selected_existing_validation") or saved.get("existing_proposal_pending"):
        return validate_existing_baseline(output, record, client)
    source = output / saved["source"]
    context, visual = baseline_context(source, output, saved)
    classified, saved = review_relevance(context, visual, output, record, client)
    if saved["status"] == "source_exclusion_validated":
        return saved
    if saved["item"]["media"] and "media_observation_validation" not in saved["phases"]:
        if client is None:
            raise ValueError("complete baseline native media observations are required")
        prepare_media_observations(source, output, record, client, _privacy_observation_context)
        saved = json.loads(record.read_bytes())
    if "media_observation_validation" in saved["phases"]:
        context["confirmed_media_values"] = confirmed_media_values(
            saved, privacy_context(context), visual
        )
    context["relevance_decision"] = classified.model_dump()
    feedback = list(saved.get("privacy_feedback", []))
    while len(feedback) <= LIMIT:
        validator = None
        attempt = output / "attempts" / record.stem / f"attempt-{len(feedback):03d}"
        editor_body = _request(EDITOR_MODEL, "privacy", context, visual, PrivacyDecision, feedback)
        editor = None
        try:
            editor = _execute(attempt / "editor.json", editor_body, client)
            proposal = parse_response(
                editor, PrivacyDecision, EDITOR_MODEL, request_hash(editor_body)
            )
            saved["phases"]["privacy"] = editor.model_dump(mode="json")
            saved["privacy_feedback"] = feedback
            atomic_private(record, json_bytes(saved))
            candidate, candidate_visual, projected, files = _compile_admission(
                source, output, record, context, visual, proposal, client
            )
            saved = json.loads(record.read_bytes())
            validator_body = _validation_request(
                context, candidate, proposal, visual + candidate_visual, feedback
            )
            validator = _execute(attempt / "validator.json", validator_body, client)
            checked = parse_response(
                validator, AdmissionValidation, VALIDATOR_MODEL, request_hash(validator_body)
            )
            if checked.approved or checked.retirement_valid:
                raise ValueError(
                    "privacy-only validation cannot claim retirement or publication approval"
                )
            if checked.output_sha256 != candidate["output_sha256"]:
                raise ValueError("stale baseline source privacy output_sha256")
            if not checked.relevance_valid:
                raise ValueError("independent baseline source privacy relevance_valid=false")
            saved["privacy_validation_evidence"] = validator.model_dump(mode="json")
            saved["privacy_validation_binding_sha256"] = context["binding_sha256"]
            if not independently_validated_privacy(saved):
                failures = {
                    "failed_checks": [
                        key
                        for key, value in checked.model_dump().items()
                        if isinstance(value, bool)
                        and not value
                        and key not in {"approved", "retirement_valid"}
                    ],
                    "failures": checked.failures,
                    "unresolved": checked.unresolved,
                }
                raise ValueError(
                    "independent baseline privacy, complete media or preservation failed: "
                    + json.dumps(failures, ensure_ascii=False)
                    + ". Correct the exact reported defects. Pending downstream retirement/API "
                    "and publication checks do not belong in this privacy-only result's failures "
                    "or unresolved arrays; they remain mandatory separate later gates."
                )
            enforce(candidate["findings"], projected, checked)
            saved.update(
                status="privacy_only_validated",
                privacy_feedback=feedback,
                candidate=candidate,
                privacy_decision=proposal.model_dump(),
                enforcement_findings=0,
                retirement_review_required=True,
                privacy_retryable=False,
            )
            for name, data in files.items():
                atomic_private(output / "reviewed" / record.stem / name, data)
            atomic_private(record, json_bytes(saved))
            return saved
        except TransportFailure as failure:
            retryable = getattr(failure, "retryable", True)
            _save(
                attempt / "transport.private.json", {"error": str(failure), "retryable": retryable}
            )
            saved = json.loads(record.read_bytes())
            saved.update(
                status="privacy_review_required",
                privacy_feedback=feedback,
                privacy_retryable=retryable,
            )
            atomic_private(record, json_bytes(saved))
            raise
        except ValueError as failure:
            atomic_private(attempt / "failure.private.json", json_bytes({"error": str(failure)}))
            if client is None:
                raise
            _invalidate_attempt(client, editor, validator)
            feedback = [*feedback, str(failure)]
            saved.update(
                status="privacy_review_required", privacy_feedback=feedback, privacy_retryable=False
            )
            atomic_private(record, json_bytes(saved))
    raise ValueError(
        "baseline source privacy exhausted two corrections; prerequisite remains queued"
    )


def _invalidate_attempt(client: Any, *responses: Any) -> None:
    invalidate = getattr(client, "invalidate", None)
    if invalidate is not None:
        for response in responses:
            if response is not None:
                invalidate(response)


def verify_baseline_prerequisite(output: Path, context: dict[str, Any]) -> None:
    """Verify the privacy intermediate and model evidence separately from final curation."""
    evidence = context.get("evidence", {})
    if evidence.get("input_kind") == "independently_admitted_current_source":
        # pylint: disable-next=import-outside-toplevel
        from .admitted_baseline_sources import (
            verify_replacement,  # pylint: disable=import-outside-toplevel
        )

        verify_replacement(context, output)
        return
    if evidence.get("source_disposition_kind") == "source_relevance_exclusion":
        # pylint: disable-next=import-outside-toplevel
        from .baseline_dispositions import (
            replay_exclusion,  # pylint: disable=import-outside-toplevel
        )

        replay_exclusion(context, output)
        return
    pin = evidence.get("baseline_source_privacy_record_sha256")
    if pin is None:
        return
    article = evidence["baseline_article"]
    privacy = output / "dependency-privacy"
    record = privacy / "documents" / (sha(article) + ".json")
    if record.is_symlink() or sha(record.read_bytes()) != pin:
        raise ValueError("baseline source privacy evidence changed")
    import os  # pylint: disable=import-outside-toplevel

    # pylint: disable-next=import-outside-toplevel
    from .baseline_global_ledgers import verify_strongest_ledger

    history = Path(os.environ.get("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(output)))
    verify_strongest_ledger(privacy, record, history)
    saved = review_baseline_privacy(privacy, record)
    candidate = saved["candidate"]
    if not all(
        (
            saved["status"] == "privacy_only_validated",
            candidate["metadata"] == context["metadata"],
            candidate["body"] == context["body"],
            candidate["media"] == context["media"],
            candidate["output_sha256"] == context["source_files_sha256"],
            saved["binding"]["baseline_receipt_sha256"] == evidence["baseline_receipt_sha256"],
        )
    ):
        raise ValueError("baseline source privacy input or intermediate mismatch")


def _privacy_observation_context(
    capture: Path, output: Path, saved: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context, visual = baseline_context(capture, output, saved)
    return privacy_context(context), visual


def review_baseline_batch(
    output: Path, inventory: dict[str, Any], journal: Path, *, replay: bool = False
) -> dict[str, Any]:
    client = None if replay else client_for_host(journal, concurrency=4, local_client=CodexClient)

    def run(item: dict[str, Any]) -> dict[str, Any]:
        try:
            if replay:
                from .baseline_global_ledgers import (  # pylint: disable=import-outside-toplevel
                    history_root,
                    verify_strongest_ledger,
                )

                verify_strongest_ledger(output, output / item["path"], history_root(journal))
                reviewed = review_baseline_privacy(output, output / item["path"], client)
            else:
                from .baseline_global_ledgers import (  # pylint: disable=import-outside-toplevel
                    history_root,
                    reconcile_record,
                    source_lock,
                )

                root = history_root(journal)
                record = output / item["path"]
                with source_lock(record, root):
                    reconcile_record(output, record, root, _execution_digest())
                    reviewed = review_baseline_privacy(output, record, client)
            return {**item, "disposition": reviewed["status"]}
        except (ValueError, KeyError, OSError) as error:
            return {**item, "disposition": "queued", "error_class": type(error).__name__}

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, inventory["documents"]))
    finally:
        if client is not None:
            client.close()
    report = {
        "documents": results,
        "exceptions": sum(
            d["disposition"] not in {"privacy_only_validated", "source_exclusion_validated"}
            for d in results
        ),
        "publication_approved": False,
    }
    atomic_private(output / "report.json", json_bytes(report))
    return report
