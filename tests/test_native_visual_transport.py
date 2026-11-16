"""Transport preserves logical media and binds the complete native attachment view."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from test_codex_worker import body, client
from test_native_pixel_views import equivalent_pair
from test_native_visuals import image

from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.enrichment.native_visuals import validate_visual_manifest


def test_oversized_repeated_media_uses_complete_native_view_and_validated_cache(
    tmp_path, monkeypatch
):
    request = body()
    pictures = [image(i) for i in range(24)]
    request["input"][0]["content"] += pictures + pictures + pictures[:3]
    original = deepcopy(request)
    calls = []

    def run(args, **kwargs):
        calls.append({"args": args, "text": kwargs["input"]})
        assert args.count("--image") == 24
        assert "--model" in args and args[args.index("--model") + 1] == request["model"]
        assert 'model_provider="litellm"' in args
        assert 'model_reasoning_effort="high"' in args
        assert "features.shell_tool=false" in args and 'web_search="disabled"' in args
        assert "EVERY original logical" in kwargs["input"]
        assert (
            len({Path(args[i + 1]).read_bytes() for i, v in enumerate(args) if v == "--image"})
            == 24
        )
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    worker = client(tmp_path)
    evidence = worker.execute(request)
    assert not evidence.error and evidence.request == original and request == original
    manifest = evidence.response["native_visual_manifest"]
    assert len(validate_visual_manifest(request, manifest)) == 51
    assert evidence.response["native_prompt_sha256"] == sha(calls[0]["text"])
    attempt = json.loads(next(tmp_path.glob("*.attempt-*.json")).read_bytes())
    assert attempt["raw"]["native_visual_manifest"] == manifest
    assert worker.execute(request) == evidence and len(calls) == 1
    tampered = evidence.model_copy(deep=True)
    tampered.response["native_visual_manifest"]["references"].pop()
    tampered.response_sha256 = sha(
        json.dumps(tampered.response, sort_keys=True, separators=(",", ":"))
    )
    (tmp_path / (request_hash(request) + ".codex.json")).write_bytes(
        json_bytes(tampered.model_dump())
    )
    assert worker.execute(request) == evidence and len(calls) == 1
    assert len(list(tmp_path.glob("*.rejected-*.json"))) == 1


def test_normal_media_retains_previous_prompt_arguments_and_response_format(tmp_path, monkeypatch):
    request = body()
    request["input"][0]["content"] += [image(1), image(1), image(2)]

    def run(args, **kwargs):
        assert args.count("--image") == 3
        assert kwargs["input"] == "untrusted article"
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    evidence = client(tmp_path).execute(request)
    assert not evidence.error and "native_visual_manifest" not in evidence.response


def test_encoding_equivalent_native_transport_preserves_failed_attempts_and_validates_cache(
    tmp_path, monkeypatch
):
    request = body()
    request["input"][0]["content"] += list(equivalent_pair())
    before = deepcopy(request)
    calls = []
    monkeypatch.setattr("html_to_markdown.enrichment.native_visuals.MAX_DECODED_BYTES", 64)

    def run(args, **kwargs):
        calls.append(args)
        assert args.count("--image") == 1
        assert "COMPLETE decoded pixels" in kwargs["input"]
        assert "Review EACH source" in kwargs["input"]
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    worker = client(tmp_path)
    evidence = worker.execute(request)
    assert not evidence.error and evidence.request == request == before
    manifest = evidence.response["native_visual_manifest"]
    assert manifest["schema_version"] == 2 and len(validate_visual_manifest(request, manifest)) == 2
    assert worker.execute(request) == evidence and len(calls) == 1
    tampered = evidence.model_copy(deep=True)
    tampered.response["native_visual_manifest"]["references"][1]["image_bytes_sha256"] = "0" * 64
    tampered.response_sha256 = sha(
        json.dumps(tampered.response, sort_keys=True, separators=(",", ":"))
    )
    (tmp_path / (request_hash(request) + ".codex.json")).write_bytes(
        json_bytes(tampered.model_dump())
    )
    assert worker.execute(request) == evidence and len(calls) == 1
    rejected = next(tmp_path.glob("*.rejected-*.json"))
    assert json.loads(rejected.read_bytes())["response"] == tampered.response
