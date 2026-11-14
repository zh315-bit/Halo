"""LLM shell client (AGENTS.md §10). Provider is still open (⚑ Q8), so only the interface, a NullLLM and a
FakeLLM for tests exist. A real adapter plugs in behind `get_llm()` once the provider and key are known.

Offline guarantee: every call goes through `call_llm()`, which returns None *before touching the client*
when the mode is offline or the client is unavailable. Nothing in the shell opens a connection by itself.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
from typing import Callable, Optional, Protocol, Union

log = logging.getLogger("halo.shell.llm")
MAX_TIMEOUT_S = 8.0


class LLMClient(Protocol):
    name: str

    def available(self) -> bool: ...

    def complete(self, system: str, user: str, timeout_s: float) -> str: ...


class NullLLM:
    """No provider / no key: never available, never called."""
    name = "none"

    def __init__(self, why: str = "no LLM configured"):
        self.why = why

    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str, timeout_s: float) -> str:  # pragma: no cover - guarded
        raise RuntimeError(f"NullLLM called ({self.why})")


Reply = Union[str, Exception, Callable[[str, str], str]]


class FakeLLM:
    """Scripted client for tests. `replies` are consumed in order; an Exception is raised; a callable gets
    (system, user). `calls` records every prompt, so tests can assert that nothing was sent."""
    name = "fake"

    def __init__(self, *replies: Reply, available: bool = True):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self._available = available

    def available(self) -> bool:
        return self._available

    def complete(self, system: str, user: str, timeout_s: float) -> str:
        self.calls.append({"system": system, "user": user, "timeout_s": timeout_s})
        if not self.replies:
            raise RuntimeError("FakeLLM: no scripted reply left")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(system, user) if callable(r) else r


def timeout_s() -> float:
    try:
        return min(MAX_TIMEOUT_S, float(os.environ.get("HALO_LLM_TIMEOUT_S", MAX_TIMEOUT_S)))
    except ValueError:
        return MAX_TIMEOUT_S


def get_llm() -> LLMClient:
    """Provider from env. Until ⚑ Q8 is decided every value maps to NullLLM (the flow runs without an LLM)."""
    provider = (os.environ.get("HALO_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        return NullLLM()
    if not os.environ.get("HALO_LLM_API_KEY"):
        return NullLLM(f"HALO_LLM_PROVIDER={provider} but HALO_LLM_API_KEY is empty")
    return NullLLM(f"no adapter for provider `{provider}` yet (⚑ Q8)")


_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="halo-llm")


def call_llm(llm: LLMClient, system: str, user: str, mode: str) -> Optional[str]:
    """The single gate to the LLM. Returns None (fallback) when offline, unavailable, on timeout or error."""
    if mode == "offline":
        return None
    try:
        if not llm.available():
            return None
    except Exception:
        return None
    t = timeout_s()
    fut = _pool.submit(llm.complete, system, user, t)
    try:
        out = fut.result(timeout=t)
    except concurrent.futures.TimeoutError:
        log.warning("LLM timeout after %.1fs", t)
        return None
    except Exception as e:
        log.warning("LLM error: %s: %s", type(e).__name__, e)
        return None
    return out if isinstance(out, str) and out.strip() else None


def extract_json(text: str):
    """First JSON object/array in an LLM reply (tolerates ```json fences and chatter). None if absent/invalid."""
    m = re.search(r"[\[{]", text or "")
    if not m:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[m.start():])[0]
    except ValueError:
        return None
