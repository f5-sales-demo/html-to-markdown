"""MacBook-installed Codex transport through its configured LiteLLM provider."""

import base64
import fcntl
import json
import platform
import re
import shutil
import subprocess  # nosec B404
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError

from ..curation import json_bytes
from ..private_storage import StorageLease, StorageUnavailable, atomic_write, private_scratch
from .analysis import sha
from .contracts import PROMPT_VERSION, REASONING, ResponseEvidence
from .gates import request_hash
from .model_policy import validate_new_request
from .native_visuals import native_visual_view, validate_visual_manifest, visual_manifest_text
from .visual_capacity import MAX_DECODED_BYTES, decoded_image_bytes

DISABLED = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "multi_agent",
    "goals",
    "view_image",
    "computer_use",
    "browser_use",
    "memories",
    "hooks",
    "sleep_tool",
    "image_generation",
    "code_mode_host",
    "skill_search",
)


def is_transient_error(error: str | None) -> bool:
    if not error:
        return False
    # Typed permanent failures outrank incidental numbers or document vocabulary.
    if re.search(
        r"invalid_request_error|BadRequestError|invalid_json_schema|failed validating|"
        r"decoded image capacity|resolved image size|model refusal|context.compact|"
        r"is not of type|is not one of|SchemaValidationError|insufficient_quota",
        error,
        re.I,
    ):
        return False
    statuses = re.findall(
        r"(?:HTTP(?:\s+(?:status|error|code))?\s*[:=]?\s*|"
        r"[\"'](?:status_code|status|code)[\"']\s*:\s*[\"']?)([45]\d\d)\b",
        error,
        re.I,
    )
    if statuses:
        return all(int(code) == 429 or 500 <= int(code) < 600 for code in statuses)
    if re.search(r"\b429\s+(?:too many requests|rate limit)\b", error, re.I):
        return True
    return bool(
        re.search(
            r"\b(?:timeout|timed out|ReadTimeout|ConnectTimeout|ConnectError|"
            r"ConnectionError|ConnectionResetError|NetworkError|connection reset|"
            r"connection refused|network error|"
            r"temporarily unavailable|stream disconnected)\b",
            error,
            re.I,
        )
    )


class CodexClient:
    """Run isolated structured document workers; never forward scraped text to tools."""

    def __init__(self, journal: Path, concurrency: int = 4) -> None:
        if platform.system() != "Darwin":
            raise ValueError("inference is routed exclusively to Codex installed on the MacBook")
        self.executable = shutil.which("codex")
        if self.executable is None:
            raise ValueError("installed Codex CLI is unavailable")
        config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
        provider = config.get("model_provider")
        route = config.get("model_providers", {}).get(provider, {})
        if provider != "litellm" or route.get("wire_api") != "responses":
            raise ValueError("configured Codex route must be LiteLLM with Responses")
        self.route = {key: route[key] for key in ("name", "base_url", "env_key", "wire_api")}
        self.version = subprocess.run(
            [self.executable, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()  # nosec B603
        self.journal = journal
        journal.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.concurrency = concurrency
        if not 1 <= concurrency <= 4:
            raise ValueError("content inference supports at most four concurrent jobs")
        lock_path = Path.home() / ".local" / "share" / "html-to-markdown" / "inference.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = lock_path.open("a+b")
        lock_path.chmod(0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as failure:
            self.lock.close()
            raise ValueError(
                "another content inference worker holds the single-worker lock"
            ) from failure

    def run(
        self, requests: dict[str, dict[str, Any]], *, wait: bool = False
    ) -> dict[str, ResponseEvidence]:
        del wait  # Local queue completes submitted work; the CLI can be interrupted and resumed.
        results = {}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self.execute, body): identity
                for identity, body in sorted(requests.items())
            }
            for future in as_completed(futures):
                identity = futures[future]
                results[identity] = future.result()
                print(
                    f"Codex {len(results)}/{len(requests)}: {identity} "
                    f"{'failed' if results[identity].error else 'completed'}",
                    flush=True,
                )
        return results

    # Keep the complete request, invocation and receipt in one worker transaction.
    # pylint: disable-next=too-many-locals
    def execute(self, body: dict[str, Any]) -> ResponseEvidence:
        last: ResponseEvidence | None = None
        for attempt in range(3):
            last = self._execute_once(body)
            if not is_transient_error(last.error):
                return last
            if attempt < 2:
                time.sleep(2**attempt)
        if last is None:
            raise RuntimeError("Codex worker did not record an attempt")
        return last

    # Preserve all stage evidence and checks in one transaction.
    # pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
    def _execute_once(self, body: dict[str, Any]) -> ResponseEvidence:
        digest = request_hash(body)
        cache = self.journal / f"{digest}.codex.json"
        if cache.is_file():
            try:
                evidence = ResponseEvidence.model_validate_json(cache.read_bytes())
                if evidence.request_sha256 != digest or request_hash(evidence.request) != digest:
                    raise ValueError("stale cached Codex decision")
                if (
                    self.journal / (digest + ".rejected-" + evidence.response_sha256 + ".json")
                ).exists():
                    raise ValueError("cached Codex decision was semantically rejected")
                if evidence.error is None:
                    self._validate_cached(evidence, body)
                    return evidence
            except (ValueError, SchemaValidationError):
                pass
            rejected = self.journal / (digest + ".rejected-" + sha(cache.read_bytes()) + ".json")
            cache.rename(rejected)
        restored = self._completed_checkpoint(body, digest)
        if restored is not None:
            atomic_write(cache, json_bytes(restored.model_dump()))
            return restored
        try:
            lease = StorageLease(self.journal, len(json_bytes(body)) * 4)
        except StorageUnavailable:
            atomic_write(
                self.journal / (digest + ".storage-pending.json"),
                json_bytes(
                    {
                        "request_sha256": digest,
                        "reason": "insufficient_private_storage",
                        "inference_invoked": False,
                        "source_or_review_approval": False,
                    }
                ),
            )
            raise
        with lease:
            return self._new_evidence(body, digest, cache)

    # The storage lease is held through transport and both complete checkpoints.
    # pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
    def _new_evidence(self, body: dict[str, Any], digest: str, cache: Path) -> ResponseEvidence:
        request_path = self.journal / (digest + ".request.json")
        if request_path.exists():
            if request_hash(json.loads(request_path.read_bytes())) != digest:
                raise ValueError("private persisted request digest changed")
        else:
            atomic_write(request_path, json_bytes(body))
        attempt = self._start_attempt(digest)
        # Native files use the filesystem covered by the inference lease.
        with private_scratch(self.journal) as root:
            instructions = root / "instructions.txt"
            instructions.write_text(body["instructions"])
            schema = root / "schema.json"
            schema.write_bytes(json_bytes(body["text"]["format"]["schema"]))
            output = root / "output.json"
            args = [
                self.executable,
                "--no-daemon",
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "-C",
                str(root),
                "--model",
                body["model"],
                "-c",
                'model_provider="litellm"',
                "-c",
                f'model_reasoning_effort="{REASONING}"',
                "-c",
                f"model_instructions_file={json.dumps(str(instructions))}",
                "-c",
                'web_search="disabled"',
                "--json",
                "--output-schema",
                str(schema),
                "--output-last-message",
                str(output),
            ]
            for key, value in self.route.items():
                args += ["-c", f"model_providers.litellm.{key}={json.dumps(value)}"]
            for feature in DISABLED:
                args += ["-c", f"features.{feature}=false"]
            texts = [
                content["text"]
                for item in body["input"]
                for content in item["content"]
                if content["type"] == "input_text"
            ]
            error = None
            raw: dict[str, Any] = {"invoked": False}
            normalized: dict[str, Any] = {}
            try:
                validate_new_request(body)
                visuals, visual_manifest = native_visual_view(body)
                if visual_manifest is not None:
                    validate_visual_manifest(body, visual_manifest)
                    texts.append(visual_manifest_text(visual_manifest))
                if decoded_image_bytes(visuals) > MAX_DECODED_BYTES:
                    raise ValueError(
                        "complete physical native attachments exceed decoded image capacity"
                    )
                for content in visuals:
                    header, data = content["image_url"].split(",", 1)
                    if not header.startswith("data:image/") or ";base64" not in header:
                        raise ValueError("Codex visual input must be captured image bytes")
                    image = root / f"image-{sha(data)}.png"
                    image.write_bytes(base64.b64decode(data, validate=True))
                    args += ["--image", str(image)]
                # End options so variadic --image cannot consume the stdin prompt marker.
                args += ["--", "-"]
                raw["invoked"] = True
                process = subprocess.run(
                    args,
                    input="\n\n".join(texts),
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )  # nosec B603
                raw = {
                    "stdout": process.stdout,
                    "stderr": process.stderr,
                    "returncode": process.returncode,
                }
                events = [
                    json.loads(line)
                    for line in process.stdout.splitlines()
                    if line.strip().startswith("{")
                ]
                if visual_manifest is not None:
                    raw["native_visual_manifest"] = visual_manifest
                    raw["native_prompt_sha256"] = sha("\n\n".join(texts))
                forbidden = {
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "web_search",
                    "tool_call",
                }
                if any(e.get("item", {}).get("type") in forbidden for e in events):
                    raise ValueError("document worker attempted an execution tool")
                if any(
                    "compact" in e.get("type", "").lower()
                    or "compact" in e.get("item", {}).get("type", "").lower()
                    for e in events
                ):
                    raise ValueError("document worker lost complete context through compaction")
                completed = [e for e in events if e.get("type") == "turn.completed"]
                if process.returncode != 0 or len(completed) != 1 or not output.is_file():
                    details = [e.get("message", "") for e in events if e.get("type") == "error"]
                    raise ValueError("Codex incomplete or failed response: " + " ".join(details))
                value = json.loads(output.read_text())
                Draft202012Validator(body["text"]["format"]["schema"]).validate(value)
                usage = completed[0].get("usage", {})
                normalized = {
                    "status": "completed",
                    "model": body["model"],
                    "transport": "codex_litellm",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(value, ensure_ascii=False),
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "input_tokens_details": {
                            "cached_tokens": usage.get("cached_input_tokens", 0),
                            "cache_write_tokens": usage.get("cache_write_input_tokens", 0),
                        },
                    },
                    "events": events,
                }
                if visual_manifest is not None:
                    normalized["native_visual_manifest"] = visual_manifest
                    normalized["native_prompt_sha256"] = sha("\n\n".join(texts))
            except subprocess.TimeoutExpired as failure:
                raw = {
                    "invoked": True,
                    "timeout": failure.timeout,
                    "stdout_base64": base64.b64encode(
                        failure.stdout.encode()
                        if isinstance(failure.stdout, str)
                        else failure.stdout or b""
                    ).decode(),
                    "stderr_base64": base64.b64encode(
                        failure.stderr.encode()
                        if isinstance(failure.stderr, str)
                        else failure.stderr or b""
                    ).decode(),
                    "partial_output_base64": base64.b64encode(
                        output.read_bytes() if output.is_file() else b""
                    ).decode(),
                }
                error = str(failure)
            except (
                ValueError,
                OSError,
                SchemaValidationError,
            ) as failure:
                error = (
                    "SchemaValidationError: " + str(failure)
                    if isinstance(failure, SchemaValidationError)
                    else str(failure)
                )
            evidence = ResponseEvidence(
                request_sha256=digest,
                response_sha256=sha(json.dumps(normalized, sort_keys=True, separators=(",", ":"))),
                model=body["model"],
                reasoning=REASONING,
                prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip()
                if "Prompt version: " in body["instructions"]
                else PROMPT_VERSION,
                request=body,
                response=normalized,
                error=error,
                transport="codex_litellm",
                runtime_version=self.version,
            )
            attempt_path = self.journal / f"{digest}.attempt-{attempt:03d}.json"
            # Commit the complete transport output before duplicating the large
            # original request into attempt and cache files. It can only recover
            # with the exact separately persisted original request.
            completed_path = self.journal / f"{digest}.completed-{attempt:03d}.json"
            atomic_write(
                completed_path,
                json_bytes(
                    {
                        "evidence": evidence.model_dump(exclude={"request"}),
                        "raw": raw,
                        "request_file_sha256": sha(request_path.read_bytes()),
                    }
                ),
            )
            atomic_write(attempt_path, json_bytes({"evidence": evidence.model_dump(), "raw": raw}))
            if error is not None:
                return evidence
            atomic_write(cache, json_bytes(evidence.model_dump()))
            return evidence

    def _start_attempt(self, digest: str) -> int:
        numbers = [
            int(match.group(1))
            for path in self.journal.glob(digest + ".*")
            if (
                match := re.fullmatch(
                    re.escape(digest) + r"\.(?:started|attempt|completed)-(\d+)\.json",
                    path.name,
                )
            )
        ]
        number = max(numbers, default=0) + 1
        atomic_write(
            self.journal / f"{digest}.started-{number:03d}.json",
            json_bytes({"request_sha256": digest, "attempt": number, "approval": False}),
        )
        return number

    def _completed_checkpoint(self, body: dict[str, Any], digest: str) -> ResponseEvidence | None:
        request_path = self.journal / (digest + ".request.json")
        if not request_path.is_file():
            return None
        request_bytes = request_path.read_bytes()
        original = json.loads(request_bytes)
        if request_hash(original) != digest or request_hash(body) != digest:
            raise ValueError("recovery original request digest changed")
        for path in sorted(self.journal.glob(digest + ".completed-*.json"), reverse=True):
            try:
                checkpoint = json.loads(path.read_bytes())
                if checkpoint["request_file_sha256"] != sha(request_bytes):
                    raise ValueError("completed checkpoint original bytes changed")
                evidence = ResponseEvidence.model_validate(
                    {**checkpoint["evidence"], "request": original}
                )
                if evidence.error is not None:
                    continue
                if (
                    self.journal / (digest + ".rejected-" + evidence.response_sha256 + ".json")
                ).exists():
                    # A complete transport response can still fail independent
                    # semantic validation. Recovery never restores that approval.
                    continue
                if evidence.request_sha256 != digest:
                    raise ValueError("completed checkpoint request changed")
                self._validate_cached(evidence, body)
                return evidence
            except (ValueError, KeyError, TypeError, SchemaValidationError):
                rejected = self.journal / (
                    digest + ".rejected-completion-" + sha(path.read_bytes()) + ".json"
                )
                atomic_write(rejected, path.read_bytes())
        return None

    @staticmethod
    def _validate_cached(evidence: ResponseEvidence, body: dict[str, Any]) -> None:
        response = evidence.response
        if (
            sha(json.dumps(response, sort_keys=True, separators=(",", ":")))
            != evidence.response_sha256
        ):
            raise ValueError("cached response digest mismatch")
        if response.get("status") != "completed" or response.get("model") != body["model"]:
            raise ValueError("cached response is incomplete or wrong-model")
        events = response.get("events", [])
        forbidden = {"command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call"}
        if any(
            "compact" in e.get("type", "").lower() or e.get("item", {}).get("type") in forbidden
            for e in events
        ):
            raise ValueError("cached response used tools or context compaction")
        if "native_visual_manifest" in response:
            manifest = response["native_visual_manifest"]
            validate_visual_manifest(body, manifest)
            texts = [
                c["text"]
                for item in body["input"]
                for c in item["content"]
                if c["type"] == "input_text"
            ]
            texts.append(visual_manifest_text(manifest))
            if response.get("native_prompt_sha256") != sha("\n\n".join(texts)):
                raise ValueError("cached native visual request context changed")
        text = [
            c["text"]
            for item in response.get("output", [])
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        ]
        if len(text) != 1:
            raise ValueError("cached response has ambiguous output")
        Draft202012Validator(body["text"]["format"]["schema"]).validate(json.loads(text[0]))

    @staticmethod
    def validate_transport_response(evidence: ResponseEvidence, body: dict[str, Any]) -> None:
        """Replay the complete preserved transport checks without invoking a model."""
        CodexClient._validate_cached(evidence, body)

    def close(self) -> None:
        lock = getattr(self, "lock", None)
        if lock is not None and not lock.closed:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def invalidate(self, evidence: ResponseEvidence) -> None:
        """Preserve a semantically rejected receipt without reusing it as a cache hit."""
        cache = self.journal / (evidence.request_sha256 + ".codex.json")
        rejected = self.journal / (
            evidence.request_sha256 + ".rejected-" + evidence.response_sha256 + ".json"
        )
        if cache.exists():
            cache.replace(rejected)
        elif not rejected.exists():
            atomic_write(
                rejected,
                json_bytes(
                    {
                        "request_sha256": evidence.request_sha256,
                        "response_sha256": evidence.response_sha256,
                        "approval": False,
                        "reason": "semantic_validation_failed",
                    }
                ),
            )
