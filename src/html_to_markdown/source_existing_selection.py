"""Enumerate and seal every exact-source history without selecting by ordering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .history_streaming import HistoryStream, binding_from_stream
from .source_existing_context import _record, qualify_source_proposal

VERSION = "existing-source-selection-inputs/1"
CORE_KEYS = ("identity", "article_sha256", "source_article_html_sha256")


def _safe(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("unsafe source selection path")


def _stream(path: Path) -> tuple[dict[str, Any], str]:
    _safe(path)
    with path.open("rb") as source:
        stream = HistoryStream(source)
        binding = binding_from_stream(stream, source_keys=CORE_KEYS)
    if stream.surrogate_escapes:
        with path.open("rb") as source:
            stream = HistoryStream(source)
            binding = binding_from_stream(stream, standard_unicode=True, source_keys=CORE_KEYS)
    return binding, stream.digest.hexdigest()


def _census(roots: list[Path]) -> list[dict[str, str]]:
    rows = []
    if len(set(roots)) != len(roots):
        raise ValueError("duplicate source history roots")
    for root in sorted(roots):
        _safe(root)
        if not root.is_dir():
            raise ValueError("source history root is unavailable")
        for path in sorted(root.glob("*.json")):
            binding, digest = _stream(path)
            if path.stem != digest:
                raise ValueError("source history bytes differ from their digest address")
            rows.append(
                {"path": str(path), "sha256": digest, "core_sha256": sha(json_bytes(binding))}
            )
    return rows


def source_selection_inputs(
    capture: Path, current_record: Path, current_pin: str, history_roots: list[Path]
) -> dict[str, Any]:
    """Stream unrelated payloads; fully check only exact-source candidate records."""
    _safe(capture)
    _safe(current_record)
    current = _record(current_record, current_pin)
    wanted = {key: current["binding"][key] for key in CORE_KEYS}
    core_pin = sha(json_bytes(wanted))
    census = _census(history_roots)
    matching = [row for row in census if row["core_sha256"] == core_pin]
    matching.append({"path": str(current_record), "sha256": current_pin, "core_sha256": core_pin})
    by_pin: dict[str, dict[str, Any]] = {}
    for matched in matching:
        saved = _record(Path(matched["path"]), matched["sha256"])
        if {key: saved["binding"].get(key) for key in CORE_KEYS} != wanted:
            raise ValueError("source candidate identity differs from streaming census")
        feedback = saved.get("privacy_feedback", [])
        if not isinstance(feedback, list) or any(not isinstance(f, str) for f in feedback):
            raise ValueError("source candidate correction ledger is malformed")
        entry = by_pin.setdefault(
            matched["sha256"],
            {
                "record_sha256": matched["sha256"],
                "feedback_entries": len(feedback),
                "ledger_sha256": sha(json_bytes(feedback)),
                "has_privacy_proposal": bool(saved.get("phases", {}).get("privacy")),
                "custody": [],
            },
        )
        entry["custody"].append(matched["path"])
    strongest = max(row["feedback_entries"] for row in by_pin.values())
    candidates = sorted(by_pin.values(), key=lambda row: row["record_sha256"])
    for row in candidates:
        row["custody"] = sorted(set(row["custody"]))
        row["strongest"] = row["feedback_entries"] == strongest
    if census != _census(history_roots):
        raise ValueError("source history custody changed during selection inventory")
    _record(current_record, current_pin)
    return {
        "version": VERSION,
        "identity": current["item"]["identity"],
        "capture": str(capture),
        "current_record": str(current_record),
        "current_record_sha256": current_pin,
        "history_roots": [str(p) for p in sorted(history_roots)],
        "history_census": census,
        "source_core": wanted,
        "candidates": candidates,
        "strongest_feedback_entries": strongest,
        "input_controller_sha256": sha(Path(__file__).read_bytes()),
        "new_editor_allowances": 0,
        "admission_approved": False,
    }


def _qualifications(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    for row in inputs["candidates"]:
        summary = {"record_sha256": row["record_sha256"]}
        if not row["strongest"]:
            summary.update(
                disposition="superseded_by_stronger_ledger", reason="Original remains preserved"
            )
        else:
            try:
                checked = qualify_source_proposal(
                    Path(inputs["capture"]),
                    Path(inputs["current_record"]),
                    inputs["current_record_sha256"],
                    Path(row["custody"][0]),
                    row["record_sha256"],
                )
                summary.update(
                    disposition="inputs_qualified",
                    qualification_sha256=sha(json_bytes(checked)),
                    source_binding=checked["source_binding"],
                    original_proposal_sha256=sha(json_bytes(checked["original_proposal"])),
                    editor_corrections_consumed=checked["editor_corrections_consumed"],
                )
            except (KeyError, ValueError, OSError) as failure:
                summary.update(
                    disposition="inputs_rejected",
                    error_class=type(failure).__name__,
                    private_reason=str(failure),
                )
        summaries.append(summary)
    return summaries


def prepare_source_selection(
    capture: Path, current_record: Path, current_pin: str, history_roots: list[Path], output: Path
) -> Path:
    """Seal qualification summaries privately without copying original model/native payloads."""
    _safe(output)
    inputs = source_selection_inputs(capture, current_record, current_pin, history_roots)
    summaries = _qualifications(inputs)
    fresh = source_selection_inputs(capture, current_record, current_pin, history_roots)
    if fresh != inputs:
        raise ValueError("source selection input custody changed during qualification")
    sealed = {**inputs, "qualifications": summaries}
    raw = json_bytes(sealed)
    path = output / sha(inputs["identity"]) / (sha(raw) + ".json")
    _safe(path)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("immutable source selection input journal changed")
    else:
        atomic_private(path, raw)
    return path


def verify_source_selection(path: Path) -> dict[str, Any]:
    """Re-enumerate every consulted history; never write or repair the journal."""
    _safe(path)
    raw = path.read_bytes()
    if sha(raw) != path.stem:
        raise ValueError("source selection journal digest changed")
    saved: dict[str, Any] = json.loads(raw)
    if saved.get("version") != VERSION:
        raise ValueError("unsupported source selection input version")
    current = source_selection_inputs(
        Path(saved["capture"]),
        Path(saved["current_record"]),
        saved["current_record_sha256"],
        [Path(p) for p in saved["history_roots"]],
    )
    if current != {key: value for key, value in saved.items() if key != "qualifications"}:
        raise ValueError("source selection custody or controller changed")
    if _qualifications(current) != saved["qualifications"]:
        raise ValueError("source qualification differs from sealed input journal")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != path.stem:
            raise ValueError("source selection journal changed during replay")
    return saved
