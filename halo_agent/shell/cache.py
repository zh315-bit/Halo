"""Persistent LLM result cache (`data/llm_cache.json`, env HALO_LLM_CACHE_FILE). Git-ignored.

Claude Opus 5.5 takes no `temperature`, so repeat questions are made consistent by caching the first model-derived
result and replaying it, across restarts too. Namespaced (`ask`, `classify`), LRU-capped per namespace, atomic
writes, thread-safe. A missing or unreadable file starts an empty cache (logged), never an error.
Only clients with `cacheable = True` (the real AnthropicLLM) are cached; test fakes are not unless they opt in.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("halo.shell.cache")
DEFAULT_FILE = Path(__file__).resolve().parents[2] / "data" / "llm_cache.json"
MAX_PER_NS = 512
FORMAT = 1


def cache_file() -> Path:
    return Path(os.environ.get("HALO_LLM_CACHE_FILE") or DEFAULT_FILE)


class LLMCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict]] = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("format") == FORMAT and isinstance(raw.get("entries"), dict):
                return {ns: dict(v) for ns, v in raw["entries"].items() if isinstance(v, dict)}
            log.warning("ignoring %s: unknown format", self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            log.warning("ignoring unreadable %s: %s", self.path, e)
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"format": FORMAT, "entries": self._data}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    def get(self, ns: str, key: str) -> Optional[dict]:
        with self._lock:
            v = self._data.get(ns, {}).get(key)
            return copy.deepcopy(v) if v is not None else None

    def put(self, ns: str, key: str, value: dict) -> None:
        with self._lock:
            d = self._data.setdefault(ns, {})
            d.pop(key, None)
            d[key] = copy.deepcopy(value)          # insertion order = LRU order
            while len(d) > MAX_PER_NS:
                d.pop(next(iter(d)))
            self._save()

    def clear(self) -> int:
        with self._lock:
            n = sum(len(v) for v in self._data.values())
            self._data = {}
            self._save()
            return n

    def size(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._data.values())


_cache: Optional[LLMCache] = None
_cache_lock = threading.Lock()


def get_cache() -> LLMCache:
    """Process-wide cache; re-opened if HALO_LLM_CACHE_FILE points somewhere else."""
    global _cache
    with _cache_lock:
        if _cache is None or _cache.path != cache_file():
            _cache = LLMCache(cache_file())
        return _cache


def reset() -> None:
    """Drop the in-memory instance (the next get_cache() re-reads the file) - simulates a restart."""
    global _cache
    with _cache_lock:
        _cache = None


def cacheable(llm) -> bool:
    return bool(getattr(llm, "cacheable", False))
