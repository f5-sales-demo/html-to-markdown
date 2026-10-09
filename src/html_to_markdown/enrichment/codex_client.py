"""MacBook-installed Codex transport through its configured LiteLLM provider."""

import base64
import json
import platform
import shutil
import subprocess  # nosec B404
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..curation import json_bytes
from .analysis import sha
from .contracts import PROMPT_VERSION, REASONING, ResponseEvidence
from .gates import request_hash

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


class CodexClient:
    """Run isolated structured document workers; never forward scraped text to tools."""

    def __init__(self, journal: Path, concurrency: int = 12) -> None:
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
        digest = request_hash(body)
        cache = self.journal / f"{digest}.codex.json"
        if cache.is_file():
            evidence = ResponseEvidence.model_validate_json(cache.read_bytes())
            if evidence.request_sha256 != digest or request_hash(evidence.request) != digest:
                raise ValueError("stale cached Codex decision")
            return evidence
        with tempfile.TemporaryDirectory(prefix="markdown-codex-") as directory:
            root = Path(directory)
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
            texts = []
            for item in body["input"]:
                for content in item["content"]:
                    if content["type"] == "input_text":
                        texts.append(content["text"])
                    elif content["type"] == "input_image":
                        header, data = content["image_url"].split(",", 1)
                        if not header.startswith("data:image/") or ";base64" not in header:
                            raise ValueError("Codex visual input must be captured image bytes")
                        image = root / f"image-{sha(data)}.png"
                        image.write_bytes(base64.b64decode(data, validate=True))
                        args += ["--image", str(image)]
            args += ["-"]
            error = None
            normalized: dict[str, Any] = {}
            try:
                process = subprocess.run(
                    args,
                    input="\n\n".join(texts),
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )  # nosec B603
                events = [
                    json.loads(line)
                    for line in process.stdout.splitlines()
                    if line.strip().startswith("{")
                ]
                forbidden = {
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "web_search",
                    "tool_call",
                }
                if any(e.get("item", {}).get("type") in forbidden for e in events):
                    raise ValueError("document worker attempted an execution tool")
                completed = [e for e in events if e.get("type") == "turn.completed"]
                if process.returncode != 0 or len(completed) != 1 or not output.is_file():
                    raise ValueError("Codex incomplete or failed response")
                value = json.loads(output.read_text())
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
            except (ValueError, OSError, subprocess.TimeoutExpired) as failure:
                error = str(failure)
            evidence = ResponseEvidence(
                request_sha256=digest,
                response_sha256=sha(json.dumps(normalized, sort_keys=True, separators=(",", ":"))),
                model=body["model"],
                reasoning=REASONING,
                prompt_version=PROMPT_VERSION,
                request=body,
                response=normalized,
                error=error,
                transport="codex_litellm",
                runtime_version=self.version,
            )
            temporary = cache.with_suffix(".tmp")
            temporary.write_bytes(json_bytes(evidence.model_dump()))
            temporary.chmod(0o600)
            temporary.replace(cache)
            return evidence

    def close(self) -> None:
        pass
