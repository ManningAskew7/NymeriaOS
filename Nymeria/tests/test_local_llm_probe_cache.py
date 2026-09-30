"""Local-server probes: a CLIProxy is never probed, and a real "no" is cached (#152).

A slim install reaches its CLIProxy on a loopback port, which passes
``is_local_llm_base_url``; the probes then asked it for Ollama tags,
llama.cpp props and vLLM versions on every LLM config build, because a
negative answer (None) was indistinguishable from a cache miss. Requests are
counted at the transport, so these tests see real traffic, not call shapes.
"""

from __future__ import annotations

from typing import Callable

import httpx
import pytest
from nymeria.config import local_llm


@pytest.fixture(autouse=True)
def _fresh_cache():
    local_llm._CACHE.clear()
    yield
    local_llm._CACHE.clear()


def _route(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[str]:
    """Send every local_llm httpx.Client through ``handler``; returns the URL log."""
    seen: list[str] = []
    real_client = httpx.Client

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recording)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(local_llm.httpx, "Client", client)
    return seen


def _not_found(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"error": "not found"})


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8318", "http://127.0.0.1:8317/v1", "http://host.docker.internal:8318"],
)
def test_a_cliproxy_base_url_is_never_probed(monkeypatch, base_url):
    seen = _route(monkeypatch, _not_found)
    assert local_llm.detect_local_server_type(base_url, api_key="cpx-gate") is None
    assert local_llm.query_local_context_length("gpt-5.5", base_url, api_key="cpx-gate") is None
    assert seen == []


def test_a_negative_answer_is_cached(monkeypatch):
    seen = _route(monkeypatch, _not_found)
    base = "http://localhost:1234"
    assert local_llm.detect_local_server_type(base) is None
    first = len(seen)
    assert first > 0
    assert local_llm.detect_local_server_type(base) is None
    assert local_llm.query_local_context_length("m", base) is None
    after_context = len(seen)
    assert local_llm.query_local_context_length("m", base) is None
    # Second calls answer from the cache: no new traffic for either probe.
    assert after_context > first  # the context probe did run once
    assert len(seen) == after_context


def test_a_server_that_never_answered_is_probed_again(monkeypatch):
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    seen = _route(monkeypatch, refused)
    base = "http://localhost:11434"
    assert local_llm.detect_local_server_type(base) is None
    first = len(seen)
    assert first > 0
    assert local_llm.detect_local_server_type(base) is None
    assert len(seen) == 2 * first


def test_a_positive_answer_is_still_cached(monkeypatch):
    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404)

    seen = _route(monkeypatch, ollama)
    base = "http://localhost:11434"
    assert local_llm.detect_local_server_type(base) == "ollama"
    first = len(seen)
    assert local_llm.detect_local_server_type(base) == "ollama"
    assert len(seen) == first


def _advance(monkeypatch, seconds: float) -> None:
    later = local_llm.time.monotonic() + seconds
    monkeypatch.setattr(local_llm.time, "monotonic", lambda: later)


def test_a_negative_expires_sooner_than_a_positive(monkeypatch):
    """A "no" lives a minute, a "yes" five: a server that answered one probe
    while the identifying one timed out is re-asked soon."""
    def ollama_on_one_port(request: httpx.Request) -> httpx.Response:
        if request.url.port == 11434 and request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404)

    seen = _route(monkeypatch, ollama_on_one_port)
    negative, positive = "http://localhost:1234", "http://localhost:11434"
    assert local_llm.detect_local_server_type(negative) is None
    assert local_llm.detect_local_server_type(positive) == "ollama"
    before = list(seen)
    _advance(monkeypatch, local_llm.LOCAL_NEGATIVE_CACHE_TTL_SECONDS + 1)
    assert local_llm.detect_local_server_type(negative) is None
    assert local_llm.detect_local_server_type(positive) == "ollama"
    new = seen[len(before):]
    assert new and all(":1234/" in url for url in new)
    assert local_llm.LOCAL_NEGATIVE_CACHE_TTL_SECONDS < local_llm.LOCAL_METADATA_CACHE_TTL_SECONDS


def test_ollama_and_llamacpp_context_negatives_are_cached(monkeypatch):
    seen = _route(monkeypatch, _not_found)
    base = "http://localhost:11434"
    assert local_llm.query_ollama_num_ctx("llama3", base) is None
    assert local_llm.query_llamacpp_context_length(base) is None
    first = len(seen)
    assert first > 0
    assert local_llm.query_ollama_num_ctx("llama3", base) is None
    assert local_llm.query_llamacpp_context_length(base) is None
    assert len(seen) == first
