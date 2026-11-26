"""Bounded process-local reuse of complete, successfully encoded raster frames."""

import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

FrameInputs = list[tuple[str, dict[str, Any]]]
FrameKey = tuple[str, int]


class ExpectedFrameCache:
    """Retain immutable serialized results, never caller-owned mutable metadata."""

    def __init__(self, *, max_bytes: int = 256 * 1024 * 1024, max_entries: int = 512) -> None:
        for limit in (max_bytes, max_entries):
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError("expected frame cache limits must be positive integers")
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._entries: OrderedDict[FrameKey, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()
        # Fixed stripes prevent simultaneous duplicate encoding without an
        # unbounded per-source lock map or serializing every unrelated image.
        self._generation_locks = tuple(threading.Lock() for _ in range(16))

    @property
    def size_bytes(self) -> int:
        """Total retained serialized bytes, excluding fixed bounded bookkeeping."""
        with self._lock:
            return self._size

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def _lookup(self, key: FrameKey) -> bytes | None:
        with self._lock:
            payload = self._entries.get(key)
            if payload is not None:
                self._entries.move_to_end(key)
            return payload

    def _store(self, key: FrameKey, payload: bytes) -> None:
        if len(payload) > self._max_bytes:
            return
        with self._lock:
            while self._entries and (
                self._size + len(payload) > self._max_bytes
                or len(self._entries) >= self._max_entries
            ):
                _, retired = self._entries.popitem(last=False)
                self._size -= len(retired)
            self._entries[key] = payload
            self._size += len(payload)

    def get_or_create(
        self, digest: str, encoding_version: int, generate: Callable[[], FrameInputs]
    ) -> FrameInputs:
        """The caller hashes current original bytes before supplying a complete generator."""
        key = (digest, encoding_version)
        payload = self._lookup(key)
        if payload is None:
            stripe = self._generation_locks[int(digest[:8], 16) % len(self._generation_locks)]
            with stripe:
                payload = self._lookup(key)
                if payload is None:
                    # Exceptions propagate and create no cache entry. Encoding
                    # and serialization must both finish before retention.
                    payload = json.dumps(generate(), ensure_ascii=True).encode("utf-8")
                    self._store(key, payload)
        # Fresh objects prevent any document's metadata edits from changing a
        # later expected frame. Tuples retain the public frame-input schema.
        return [(frame[0], frame[1]) for frame in json.loads(payload)]
