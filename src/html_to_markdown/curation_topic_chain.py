"""Closed original content inputs for independently replayed topic transformations."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from .content_inventory import atomic_private
from .curation_paths import corpus_location
from .enrichment.analysis import sha

PREFIX = ".curation-topic-inputs"
RELATIVE = re.compile(r"content/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")


def json_bytes(value: Any) -> bytes:
    from .curation import json_bytes as encode  # pylint: disable=import-outside-toplevel

    return encode(value)


def corpus_digest(output: Path) -> str:
    from .curation import corpus_digest as digest  # pylint: disable=import-outside-toplevel

    return digest(output)


def _content_files(output: Path) -> dict[str, bytes]:
    content = output / "content"
    if content.is_symlink():
        raise ValueError("topic source content cannot be a symlink")
    files = {}
    for path in sorted(content.rglob("*")):
        if path.is_symlink():
            raise ValueError("topic source includes a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        if not RELATIVE.fullmatch(relative) or ".." in Path(relative).parts:
            raise ValueError("unsafe topic source content path")
        files[relative] = path.read_bytes()
    return files


def preserve_inputs(output: Path, policy: Any, api: Any) -> dict[str, Any]:
    """Preserve complete original metadata, bodies and media under an exact binding."""
    files = _content_files(output)
    binding = {
        "schema_version": 1,
        "policy_sha256": policy.sha256,
        "api_policy_sha256": api.policy_digest,
        "api_catalog_sha256": api.catalog_digest,
        "corpus_sha256": corpus_digest(output),
        "files": {name: sha(data) for name, data in files.items()},
    }
    identity = sha(json_bytes(binding))
    root = output / PREFIX / identity
    for name, data in files.items():
        destination = root / "source" / name
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != data:
                raise ValueError("preserved topic source changed")
        else:
            atomic_private(destination, data)
    manifest = root / "input.json"
    encoded = json_bytes(binding)
    if manifest.exists():
        if manifest.is_symlink() or manifest.read_bytes() != encoded:
            raise ValueError("preserved topic input binding changed")
    else:
        atomic_private(manifest, encoded)
    return {"identity": identity, "binding": binding}


def verify_inputs(output: Path, record: dict[str, Any], policy: Any, api: Any) -> dict[str, bytes]:
    """Verify every original byte and source boundary before replaying any transformation."""
    binding = record["binding"]
    identity = record["identity"]
    if not re.fullmatch("[0-9a-f]{64}", identity) or sha(json_bytes(binding)) != identity:
        raise ValueError("topic input binding digest mismatch")
    if (
        binding.get("schema_version") != 1
        or binding.get("policy_sha256") != policy.sha256
        or binding.get("api_policy_sha256") != api.policy_digest
        or binding.get("api_catalog_sha256") != api.catalog_digest
    ):
        raise ValueError("stale topic input policy")
    root = output / PREFIX / identity
    for path in [output / PREFIX, root, root / "source", root / "input.json"]:
        if path.is_symlink():
            raise ValueError("unsafe preserved topic source")
    if (root / "input.json").read_bytes() != json_bytes(binding):
        raise ValueError("preserved topic input manifest changed")
    files = _content_files(root / "source")
    if {name: sha(data) for name, data in files.items()} != binding["files"]:
        raise ValueError("preserved topic source inventory changed")
    if corpus_digest(root / "source") != binding["corpus_sha256"]:
        raise ValueError("preserved topic corpus changed")
    return files


# Replay state is local to a verification call and never shared across worker jobs.

REPLAYING: ContextVar[bool] = ContextVar("topic_chain_replaying", default=False)
CACHE: ContextVar[tuple[Path, dict[str, Any]] | None] = ContextVar(
    "topic_chain_cache", default=None
)


def _api(api: Any) -> Any:
    if api is None:
        from .content_policy import load_content_policy  # pylint: disable=import-outside-toplevel

        return load_content_policy()
    return api


def _state_context(store: Any, files: dict[str, bytes]) -> dict[str, Any]:
    from .render import split_document  # pylint: disable=import-outside-toplevel

    links = {}
    excluded = []
    if store is not None:
        for name, data in files.items():
            if name.endswith("/index.md"):
                metadata, _ = split_document(data.decode())
                links[metadata["url"]] = list(store.candidate_links(metadata["url"]))
        tables = {
            r[0]
            for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "content_exclusions" in tables:
            excluded = [
                list(r)
                for r in store.connection.execute(
                    "SELECT url,origin FROM content_exclusions ORDER BY url,origin"
                )
            ]
    return {"links": links, "exclusions": excluded}


class ReplayStore:
    def __init__(self, context: dict[str, Any]) -> None:
        self.links = context["links"]
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE pages(canonical_url TEXT)")
        self.connection.execute("CREATE TABLE content_exclusions(url TEXT,origin TEXT)")
        self.connection.executemany(
            "INSERT INTO content_exclusions VALUES (?,?)", context["exclusions"]
        )

    def candidate_links(self, url: str) -> list[str]:
        return list(self.links.get(url, []))


def begin(output: Path, policy: Any, api: Any, store: Any = None) -> dict[str, Any] | None:
    if REPLAYING.get() or policy.legacy is None or not policy.legacy.decisions:
        return None
    api = _api(api)
    from .render import split_document  # pylint: disable=import-outside-toplevel

    files = _content_files(output)
    if not any(
        policy.legacy.canonical(str(split_document(data.decode())[0]["url"]))
        in policy.legacy.decisions
        for name, data in files.items()
        if name.endswith("/index.md")
    ):
        return None
    record = preserve_inputs(output, policy, api)
    record["state_context"] = _state_context(store, _content_files(output))
    return record


def seal(output: Path, record: dict[str, Any] | None, result: dict[str, Any]) -> None:
    if record is None:
        return
    record = {
        **record,
        "outputs": {n: sha(b) for n, b in _content_files(output).items()},
        "result": {k: v for k, v in result.items() if k != "_planned"},
    }
    raw = json_bytes(record)
    target = output / PREFIX / "record.json"
    if target.exists() and target.read_bytes() != raw:
        raise ValueError("existing topic replay record cannot be replaced")
    atomic_private(target, raw)
    atomic_private(
        output / PREFIX / "registration.json",
        json_bytes({"schema_version": 1, "record_sha256": sha(raw)}),
    )
    result["topic_input_sha256"] = sha(raw)


def _read_record(output: Path, policy: Any, api: Any) -> dict[str, Any] | None:
    target = output / PREFIX / "record.json"
    if not target.exists():
        return None
    registration = output / PREFIX / "registration.json"
    if target.is_symlink() or registration.is_symlink() or (output / PREFIX).is_symlink():
        raise ValueError("unsafe topic replay record")
    raw = target.read_bytes()
    if json.loads(registration.read_bytes()) != {"schema_version": 1, "record_sha256": sha(raw)}:
        raise ValueError("unregistered or changed topic replay record")
    audit_path = output / "curation-audit.json"
    if audit_path.exists():
        audit = json.loads(audit_path.read_bytes())
        pin = audit.get("topics", {}).get("topic_input_sha256")
        if pin is not None and pin != sha(raw):
            raise ValueError("topic replay record differs from its preserved audit")
    record = json.loads(raw)
    if not isinstance(record, dict) or not {
        "identity",
        "binding",
        "state_context",
        "outputs",
        "result",
    }.issubset(record):
        raise ValueError("malformed topic replay record")
    verify_inputs(output, record, policy, _api(api))
    return cast(dict[str, Any], record)


def _record(output: Path, policy: Any, api: Any) -> dict[str, Any] | None:
    try:
        return _read_record(output, policy, api)
    except (OSError, KeyError, TypeError) as error:
        raise ValueError("missing or malformed topic replay evidence") from error


def _replay(output: Path, policy: Any, api: Any) -> dict[str, Any] | None:
    cached = CACHE.get()
    if cached is not None and cached[0] == output:
        binding = cached[1]["record"]["binding"]
        active = _api(api)
        if (
            binding["policy_sha256"] != policy.sha256
            or binding["api_policy_sha256"] != active.policy_digest
            or binding["api_catalog_sha256"] != active.catalog_digest
        ):
            raise ValueError("cached topic replay has different policy inputs")
        return cached[1]
    record = _record(output, policy, api)
    if record is None:
        return None
    from .curation import curate_topics  # pylint: disable=import-outside-toplevel
    from .render import split_document  # pylint: disable=import-outside-toplevel
    from .state import StateStore  # pylint: disable=import-outside-toplevel

    files = verify_inputs(output, record, policy, _api(api))
    with tempfile.TemporaryDirectory(prefix="private-topic-replay-") as directory:
        root = Path(directory)
        for name, data in files.items():
            atomic_private(root / name, data)
        state = ReplayStore(record["state_context"])
        token = REPLAYING.set(True)
        try:
            curate_topics(root, cast(StateStore, state), policy=policy, api_policy=_api(api))
        finally:
            REPLAYING.reset(token)
            state.connection.close()
        expected = _content_files(root)
        if {n: sha(b) for n, b in expected.items()} != record["outputs"]:
            raise ValueError("topic output differs from independently replayed original inputs")
        articles = {
            n: split_document(b.decode()) for n, b in expected.items() if n.endswith("/index.md")
        }
        originals = {
            n: split_document(b.decode()) for n, b in files.items() if n.endswith("/index.md")
        }
        return {
            "record": record,
            "articles": articles,
            "originals": originals,
            "files": expected,
        }


def _metadata(actual: dict[str, Any], expected: dict[str, Any], body: str, api: Any = None) -> None:
    from .content_policy import _description  # pylint: disable=import-outside-toplevel
    from .render import content_hash  # pylint: disable=import-outside-toplevel

    projected = {"content_hash", "related_documents", "description"}
    if actual.get("content_hash") != content_hash(body):
        raise ValueError("topic metadata content hash differs from its exact body")
    related = expected.get("related_documents", [])
    filtered = [r for r in related if not _api(api).excludes(r["canonical_url"])]
    if actual.get("related_documents", []) not in [related, filtered]:
        raise ValueError("topic metadata relationships differ from their qualified projection")
    if {k: v for k, v in actual.items() if k not in projected} != {
        k: v for k, v in expected.items() if k not in projected
    }:
        raise ValueError("topic projection changes original authored metadata")
    if actual.get("description") not in (expected.get("description"), _description(body)):
        raise ValueError("topic projection changes the grounded description")


def topic_source(
    policy: Any, path: Path, metadata: dict[str, Any], body: str, api: Any = None
) -> tuple[dict[str, Any], str] | None:
    root, relative = corpus_location(path)
    replay = _replay(root, policy, api)
    if replay is None:
        return None
    if relative not in replay["articles"]:
        raise ValueError("topic replay does not retain this document identity")
    expected_meta, expected_body = replay["articles"][relative]
    if body != expected_body:
        raise ValueError("topic body differs from its exact qualified transformation")
    _metadata(metadata, expected_meta, body, api)
    return cast(tuple[dict[str, Any], str], replay["originals"][relative])


def reapply(
    output: Path, policy: Any, api: Any, api_reader: Callable[..., Any]
) -> dict[str, Any] | None:
    if REPLAYING.get():
        return None
    replay = _replay(output, policy, api)
    if replay is None:
        return None
    from .render import split_document  # pylint: disable=import-outside-toplevel

    current = _content_files(output)
    if set(current) != set(replay["files"]):
        raise ValueError("current topic content inventory differs from its replay")
    planned = {}
    token = CACHE.set((output, replay))
    try:
        for name, data in current.items():
            if not name.endswith("/index.md"):
                if data != replay["files"][name]:
                    raise ValueError("current topic media differs from its replay")
                continue
            metadata, body = split_document(data.decode())
            expected_meta, expected_body = replay["articles"][name]
            stage = api_reader(policy, output / name, metadata, body, _api(api))
            if body != expected_body and stage is None:
                raise ValueError("current topic body differs from its replay")
            if stage is not None:
                source_meta, source_body = stage
                if source_body != expected_body:
                    raise ValueError("API input differs from the verified topic output")
                _metadata(source_meta, expected_meta, source_body, api)
            _metadata(metadata, expected_meta, body, api)
            planned[name] = {"metadata": metadata, "body": body}
    finally:
        CACHE.reset(token)
    result: dict[str, Any] = dict(replay["record"]["result"])
    result.update(
        input_sha256=corpus_digest(output),
        output_sha256=corpus_digest(output),
        topic_input_sha256=sha(json_bytes(replay["record"])),
        _planned=planned,
    )
    return result


@contextmanager
def validation_scope(
    output: Path, policy: Any, api: Any, api_reader: Callable[..., Any]
) -> Iterator[None]:
    existing = CACHE.get()
    if existing is not None and existing[0] == output:
        yield
        return
    replay = _replay(output, policy, api)
    token = CACHE.set((output, replay)) if replay is not None else None
    try:
        yield
        if replay is not None:
            reapply(output, policy, api, api_reader)
            _record(output, policy, api)
    finally:
        if token is not None:
            CACHE.reset(token)
