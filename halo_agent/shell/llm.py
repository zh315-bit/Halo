"""LLM shell client (AGENTS.md §10): the `LLMClient` interface, `NullLLM`, `FakeLLM` (tests) and `AnthropicLLM`
(Claude via the official `anthropic` SDK, optional extra `llm`). `get_llm()` picks one from the environment:
HALO_LLM_PROVIDER=anthropic + HALO_LLM_API_KEY -> AnthropicLLM (model HALO_LLM_MODEL, default claude-opus-5-5);
anything missing -> NullLLM, and every feature falls back to its deterministic path.

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

    def __init__(self, *replies: Reply, available: bool = True, cacheable: bool = False):
        self.replies = list(replies)
        self.cacheable = cacheable                 # tests opt in to the persistent cache
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


class LLMRefusal(RuntimeError):
    """The model (and its server-side fallback) declined; callers fall back like on any other error."""


class LLMTruncated(RuntimeError):
    """Output hit max_tokens; a cut-off JSON answer is never used."""


DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    """Claude through the official SDK (Messages API, beta endpoint for server-side refusal fallbacks).

    - Thinking is not configured: on Claude Opus 5.5 it is always on (adaptive) and cannot be disabled.
    - `effort` (default `low`): classify / ask are short, bounded tasks with an 8 s budget; set HALO_LLM_EFFORT to
      raise it. Claude Opus 5.5 would otherwise default to `medium`.
    - `fallbacks: "default"`: on a refusal the API re-runs the request on a fallback model inside the same call;
      if the whole chain refuses, `stop_reason == "refusal"` -> LLMRefusal -> deterministic fallback.
    - No retries inside the SDK (`max_retries=0`): a retry could not fit inside the timeout; call_llm() falls back.
    - The SDK client is created lazily on the first real call, so building this object opens no connection.
    """
    name = "anthropic"
    cacheable = True                               # results go to the persistent cache (shell/cache.py)

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, effort: str = "low", max_tokens: int = 2048,
                 client_factory: Optional[Callable[[str], object]] = None):
        self.api_key, self.model, self.effort, self.max_tokens = api_key, model, effort, max_tokens
        self._factory = client_factory or self._sdk_client
        self._client = None

    @staticmethod
    def _sdk_client(api_key: str):
        import anthropic  # optional dependency: `uv sync --extra llm`
        return anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=MAX_TIMEOUT_S)

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, timeout_s: float) -> str:
        if self._client is None:
            self._client = self._factory(self.api_key)
        resp = self._client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            timeout=min(timeout_s, MAX_TIMEOUT_S),
        )
        if resp.stop_reason == "refusal":
            raise LLMRefusal(getattr(getattr(resp, "stop_details", None), "category", None) or "refusal")
        if resp.stop_reason == "max_tokens":
            raise LLMTruncated(f"hit max_tokens={self.max_tokens}")
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


EFFORTS = ("low", "medium", "high", "xhigh", "max")
_cache: dict[tuple, LLMClient] = {}


def get_llm() -> LLMClient:
    """LLM from env. Returns NullLLM (no network ever) unless provider, key and SDK are all present."""
    provider = (os.environ.get("HALO_LLM_PROVIDER") or "").strip().lower()
    key = (os.environ.get("HALO_LLM_API_KEY") or "").strip()
    if not provider:
        return NullLLM()
    if provider != "anthropic":
        return NullLLM(f"unknown HALO_LLM_PROVIDER `{provider}` (supported: anthropic)")
    if not key:
        return NullLLM("HALO_LLM_PROVIDER=anthropic but HALO_LLM_API_KEY is empty")
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return NullLLM("anthropic SDK not installed: uv sync --extra llm")
    model = (os.environ.get("HALO_LLM_MODEL") or DEFAULT_MODEL).strip()
    effort = (os.environ.get("HALO_LLM_EFFORT") or "low").strip().lower()
    if effort not in EFFORTS:
        return NullLLM(f"HALO_LLM_EFFORT must be one of {EFFORTS}")
    k = (key, model, effort)
    if k not in _cache:
        _cache.clear()
        _cache[k] = AnthropicLLM(key, model=model, effort=effort)
    return _cache[k]


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


if __name__ == "__main__":   # smoke test: uv run --env-file .env python -m halo_agent.shell.llm
    llm = get_llm()
    print("llm:", llm.name, getattr(llm, "model", ""), getattr(llm, "why", ""))
    if llm.available():
        print("reply:", call_llm(llm, "Reply with the single word OK.", "ping", "online"))

