"""Resumable Responses Batch transport. Private journal never contains credentials."""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..curation import json_bytes
from .analysis import sha
from .contracts import PROMPT_VERSION, REASONING, ResponseEvidence
from .gates import request_hash

TERMINAL = {"completed", "failed", "expired", "cancelled"}


class BatchClient:
    def __init__(
        self,
        journal: Path,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = os.environ.get("OPENAI_API_KEY")
        if client is None and not key:
            raise ValueError(
                "OPENAI_API_KEY is required for inference; offline replay needs no key"
            )
        self.client = client or httpx.Client(
            base_url="https://api.openai.com/v1/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=120,
        )
        self.journal = journal
        self.sleep = sleep
        journal.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            try:
                response = self.client.request(method, path, **kwargs)
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response
            except httpx.TransportError:
                if attempt == 3:
                    raise
            if attempt < 3:
                self.sleep(min(2**attempt, 8))
        raise ValueError("OpenAI transient retries exhausted")

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(json_bytes(value))
        temporary.chmod(0o600)
        temporary.replace(path)

    def run(
        self, requests: dict[str, dict[str, Any]], *, wait: bool = False
    ) -> dict[str, ResponseEvidence]:
        """Submit or resume digest-bound batches; output order is never assumed.

        wait=False returns after submission/status retrieval, so callers can stay
        responsive and resume later. Large files split below the 200 MB limit.
        """
        groups: list[dict[str, dict[str, Any]]] = []
        current: dict[str, dict[str, Any]] = {}
        size = 0
        for identity, body in sorted(requests.items()):
            line_size = len(
                json_bytes(
                    {"custom_id": identity, "method": "POST", "url": "/v1/responses", "body": body}
                )
            )
            if line_size > 190_000_000:
                raise ValueError("one complete request exceeds batch file size; split at sections")
            if current and (size + line_size > 190_000_000 or len(current) >= 49000):
                groups.append(current)
                current, size = {}, 0
            current[identity] = body
            size += line_size
        if current:
            groups.append(current)
        results: dict[str, ResponseEvidence] = {}
        for group in groups:
            results.update(self._group(group, wait=wait))
        return results

    # Batch identity and output binding must be checked as one operation.
    # pylint: disable-next=too-many-locals
    def _group(
        self, requests: dict[str, dict[str, Any]], *, wait: bool
    ) -> dict[str, ResponseEvidence]:
        digest = sha(json_bytes({key: request_hash(body) for key, body in requests.items()}))
        state_path = self.journal / f"{digest}.json"
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        results_path = self.journal / f"{digest}.results.json"
        if results_path.is_file():
            cached = json.loads(results_path.read_text())
            return {key: ResponseEvidence.model_validate(value) for key, value in cached.items()}
        if not state:
            data = b"".join(
                json.dumps(
                    {"custom_id": key, "method": "POST", "url": "/v1/responses", "body": body},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
                for key, body in requests.items()
            )
            uploaded = self._call(
                "POST",
                "files",
                data={"purpose": "batch"},
                files={"file": ("requests.jsonl", data, "application/jsonl")},
            ).json()
            batch = self._call(
                "POST",
                "batches",
                json={
                    "input_file_id": uploaded["id"],
                    "endpoint": "/v1/responses",
                    "completion_window": "24h",
                    "metadata": {"request_set_sha256": digest},
                },
            ).json()
            state = {"requests_sha256": digest, "batch": batch}
            self._write(state_path, state)
        while True:
            batch = self._call("GET", "batches/" + state["batch"]["id"]).json()
            state["batch"] = batch
            self._write(state_path, state)
            print(f"Batch {batch['id']}: {batch['status']}", flush=True)
            if batch["status"] in TERMINAL or not wait:
                break
            self.sleep(30)
        if batch["status"] not in TERMINAL:
            return {}
        lines = []
        for field in ("output_file_id", "error_file_id"):
            if batch.get(field):
                lines.extend(self._call("GET", f"files/{batch[field]}/content").text.splitlines())
        rows: dict[str, Any] = {}
        for line in lines:
            row = json.loads(line)
            identity = row.get("custom_id")
            if identity not in requests or identity in rows:
                raise ValueError("batch returned unknown or duplicate custom_id")
            rows[identity] = row
        results = {}
        for identity, body in requests.items():
            row = rows.get(identity, {})
            response = row.get("response") or {}
            returned = response.get("body") or {}
            error = None
            if not row or row.get("error") or response.get("status_code") != 200:
                error = "missing_or_failed_batch_response:" + batch["status"]
            results[identity] = ResponseEvidence(
                request_sha256=request_hash(body),
                response_sha256=sha(json.dumps(returned, sort_keys=True, separators=(",", ":"))),
                model=body["model"],
                reasoning=REASONING,
                prompt_version=PROMPT_VERSION,
                request=body,
                response=returned,
                error=error,
            )
        self._write(results_path, {key: value.model_dump() for key, value in results.items()})
        return results

    def close(self) -> None:
        self.client.close()
