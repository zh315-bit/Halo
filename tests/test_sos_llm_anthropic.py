"""AnthropicLLM adapter: request shape, refusal / truncation fallback, env selection, offline = no client, and the
real SDK's wire request via an httpx2 MockTransport (no network)."""

import json
import sys
import types
from types import SimpleNamespace

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.shell import llm as llm_mod
from halo_agent.shell.llm import AnthropicLLM, NullLLM, call_llm, get_llm
from halo_agent.sos import service as service_mod
from halo_agent.sos.session_log import SessionLog


@pytest.fixture(autouse=True)
def _isolated_llm_cache(tmp_path, monkeypatch):
    """Never touch data/llm_cache.json from tests."""
    from halo_agent.shell import cache as cache_mod
    monkeypatch.setenv("HALO_LLM_CACHE_FILE", str(tmp_path / "llm_cache.json"))
    cache_mod.reset()
    yield
    cache_mod.reset()


GOOD = '[{"symptom": "breathing_difficulty", "confidence": 0.8}]'


def resp(text=GOOD, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, *replies):
        self.calls, self.replies = [], list(replies)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make(*replies, **kw):
    fc = FakeClient(*replies)
    made = []

    def factory(key):
        made.append(key)
        return fc
    return AnthropicLLM("sk-test", client_factory=factory, **kw), fc, made


def test_request_shape_for_claude_opus_5_5():
    llm, fc, made = make(resp())
    assert made == []                                          # no client until the first real call
    assert call_llm(llm, "SYS", "USER", "online") == GOOD
    (kw,) = fc.calls
    assert kw["model"] == "claude-opus-5-5" and kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"] == {"effort": "low"}
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["timeout"] <= 8
    for banned in ("thinking", "tool_choice", "temperature", "top_p", "top_k"):
        assert banned not in kw                                # 400s on Claude Opus 5.5
    assert made == ["sk-test"]


def test_only_text_blocks_are_returned():
    llm, _, _ = make(resp(text="hello"))
    assert llm.complete("s", "u", 8) == "hello"               # empty thinking block ignored


@pytest.mark.parametrize("r", [resp(stop="refusal"), resp(text='[{"symptom": "chest', stop="max_tokens"),
                               RuntimeError("APIConnectionError")])
def test_refusal_truncation_or_error_falls_back(r):
    llm, _, _ = make(r)
    assert call_llm(llm, "s", "u", "online") is None


def test_offline_never_builds_a_client():
    llm, fc, made = make(resp())
    assert call_llm(llm, "s", "u", "offline") is None
    assert made == [] and fc.calls == []


# ---------- env selection ----------
@pytest.fixture
def clean_env(monkeypatch):
    for k in ("HALO_LLM_PROVIDER", "HALO_LLM_API_KEY", "HALO_LLM_MODEL", "HALO_LLM_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm_mod, "_cache", {})
    monkeypatch.setitem(sys.modules, "anthropic", sys.modules.get("anthropic") or types.ModuleType("anthropic"))
    return monkeypatch


def test_get_llm_from_env(clean_env):
    assert isinstance(get_llm(), NullLLM)
    clean_env.setenv("HALO_LLM_PROVIDER", "anthropic")
    n = get_llm()
    assert isinstance(n, NullLLM) and "API_KEY is empty" in n.why          # the shipped .env state
    clean_env.setenv("HALO_LLM_API_KEY", "sk-test")
    a = get_llm()
    assert isinstance(a, AnthropicLLM) and a.model == "claude-opus-5-5" and a.effort == "low"
    assert get_llm() is a                                                   # cached: one SDK client
    clean_env.setenv("HALO_LLM_EFFORT", "high")
    assert get_llm().effort == "high"
    clean_env.setenv("HALO_LLM_EFFORT", "turbo")
    assert isinstance(get_llm(), NullLLM)
    clean_env.setenv("HALO_LLM_PROVIDER", "openai")
    assert isinstance(get_llm(), NullLLM)


def test_get_llm_without_sdk_is_null(clean_env):
    clean_env.setenv("HALO_LLM_PROVIDER", "anthropic")
    clean_env.setenv("HALO_LLM_API_KEY", "sk-test")
    clean_env.setitem(sys.modules, "anthropic", None)                       # import -> ImportError
    n = get_llm()
    assert isinstance(n, NullLLM) and "uv sync --extra llm" in n.why


# ---------- through the API ----------
def test_classify_through_api_uses_adapter(client, tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    llm, fc, _ = make(resp())
    routes_sos.set_service(service_mod.SosService(reading_fn=lambda c: None, score_fn=lambda c: None,
                                                  log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode))
    routes_sos.set_llm(llm)
    try:
        sid = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "free_text": "hard to breathe"}).json()["session_id"]
        out = client.post(f"/api/v1/sos/{sid}/classify", json={"free_text": "hard to breathe"}).json()
        assert out["source"] == "llm" and out["candidates"][0]["symptom"] == "breathing_difficulty"
        assert "hard to breathe" in fc.calls[0]["messages"][0]["content"]
        client.post("/api/v1/admin/mode", json={"mode": "offline"})
        assert client.post(f"/api/v1/sos/{sid}/classify", json={"free_text": "x"}).json()["source"] == "picker_fallback"
        assert len(fc.calls) == 1
    finally:
        routes_sos.set_service(None)
        routes_sos.set_llm(None)


# ---------- the real SDK, wire level, no network ----------
def test_real_sdk_wire_request():
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    seen = {}

    def handler(request):
        seen["url"], seen["headers"], seen["body"] = str(request.url), request.headers, json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": GOOD}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}})

    def factory(key):
        return anthropic.Anthropic(api_key=key, max_retries=0,
                                   http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))

    llm = AnthropicLLM("sk-test", client_factory=factory)
    assert llm.complete("SYS", "USER", 8) == GOOD
    b = seen["body"]
    assert seen["url"].endswith("/v1/messages?beta=true") or seen["url"].endswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in seen["headers"].get("anthropic-beta", "")
    assert b["model"] == "claude-opus-5-5" and b["fallbacks"] == "default"
    assert b["output_config"] == {"effort": "low"} and "thinking" not in b and "tool_choice" not in b
    assert b["system"] == "SYS" and b["messages"] == [{"role": "user", "content": "USER"}]
