"""Tests for the shared EmbeddingClient (core/embedding_client.py).

The client is the one provider-dispatch implementation behind the memory,
skills, and tool-search indexes. These tests cover the provider-aware
availability gate, the local dispatch path, the openai batch mapping and
width validation, and the never-raise + last_error contract. No network.
"""

from __future__ import annotations

import importlib.util as ilu

from nymeria.core.embedding_client import (
    GATEKEEPER_KEY_ERROR,
    NO_KEY_ERROR,
    EmbeddingClient,
)


class _FakeEncoder:
    def __init__(self, width: int = 4):
        self.width = width
        self.calls: list[list[str]] = []

    def encode(self, inputs, **kwargs):
        self.calls.append(list(inputs))
        return [[1.0] + [0.0] * (self.width - 1) for _ in inputs]


# ----------------------------------------------------------------------
# availability gate
# ----------------------------------------------------------------------

def test_gate_remote_provider_without_key():
    for provider in ("openai", "cohere", "gemini"):
        c = EmbeddingClient(provider=provider, api_key=None)
        assert c.availability_error() == NO_KEY_ERROR


def test_gate_remote_provider_with_gatekeeper_key():
    c = EmbeddingClient(provider="openai", api_key="cpx-local-test")
    assert c.availability_error() == GATEKEEPER_KEY_ERROR


def test_gate_remote_provider_with_real_key():
    c = EmbeddingClient(provider="openai", api_key="sk-real-looking")
    assert c.availability_error() is None


def test_gate_local_ignores_missing_key(monkeypatch):
    real_find_spec = ilu.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "sentence_transformers":
            return object()
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)
    c = EmbeddingClient(provider="local", api_key=None, model="fake-granite")
    assert c.availability_error() is None


def test_gate_local_without_extra_names_the_dependency(monkeypatch):
    real_find_spec = ilu.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)
    c = EmbeddingClient(provider="local", api_key=None, model="fake-granite")
    err = c.availability_error()
    assert err is not None
    assert "local-rag" in err
    assert "EMBEDDING_API_KEY" not in err


def test_gate_unknown_provider():
    c = EmbeddingClient(provider="frobnicate", api_key="k")
    assert "unknown embedding provider" in (c.availability_error() or "")


# ----------------------------------------------------------------------
# local dispatch
# ----------------------------------------------------------------------

def test_local_embed_no_network_no_key(monkeypatch):
    enc = _FakeEncoder(width=4)
    monkeypatch.setattr(EmbeddingClient, "_get_local_embedder", lambda self: enc)
    c = EmbeddingClient(provider="local", model="fake-granite", dimensions=4)

    out = c.embed_batch(["alpha", "beta"])
    assert out == [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]
    assert c.last_error is None
    assert enc.calls == [["alpha", "beta"]]


def test_local_width_mismatch_returns_none_per_item(monkeypatch):
    enc = _FakeEncoder(width=3)  # index expects 4
    monkeypatch.setattr(EmbeddingClient, "_get_local_embedder", lambda self: enc)
    c = EmbeddingClient(provider="local", model="fake-granite", dimensions=4)

    assert c.embed_batch(["alpha"]) == [None]


def test_local_encoder_failure_never_raises(monkeypatch):
    def _boom(self):
        raise RuntimeError("weights missing")

    monkeypatch.setattr(EmbeddingClient, "_get_local_embedder", _boom)
    c = EmbeddingClient(provider="local", model="fake-granite", dimensions=4)

    assert c.embed_batch(["alpha", "beta"]) == [None, None]
    assert "weights missing" in (c.last_error or "")


# ----------------------------------------------------------------------
# openai dispatch
# ----------------------------------------------------------------------

class _FakeItem:
    def __init__(self, embedding, index=None):
        self.embedding = embedding
        if index is not None:
            self.index = index


class _FakeResponse:
    def __init__(self, data):
        self.data = data


def test_openai_maps_by_item_index_and_validates_width(monkeypatch):
    c = EmbeddingClient(provider="openai", api_key="sk-x", model="m", dimensions=2)

    class _FakeEmbeddings:
        def create(self, input, **kwargs):
            # Out-of-order response plus one wrong-width item.
            return _FakeResponse([
                _FakeItem([0.5, 0.5], index=1),
                _FakeItem([1.0, 0.0, 0.0], index=0),  # wrong width -> None
            ])

    class _FakeOpenAI:
        embeddings = _FakeEmbeddings()

        def with_options(self, **kwargs):
            return self

    monkeypatch.setattr(
        EmbeddingClient, "_get_openai_client", lambda self: _FakeOpenAI()
    )
    out = c.embed_batch(["a", "b"])
    assert out == [None, [0.5, 0.5]]


def test_openai_request_failure_sets_last_error(monkeypatch):
    c = EmbeddingClient(provider="openai", api_key="sk-x", model="m", dimensions=2)

    def _boom(self):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(EmbeddingClient, "_get_openai_client", _boom)
    assert c.embed_batch(["a"]) == [None]
    assert "endpoint down" in (c.last_error or "")
    # A later successful call clears the stale error.
    monkeypatch.setattr(
        EmbeddingClient, "_embed_openai",
        lambda self, inputs, input_type, timeout: [[0.0, 1.0]],
    )
    assert c.embed_batch(["a"]) == [[0.0, 1.0]]
    assert c.last_error is None


def test_empty_and_blank_inputs():
    c = EmbeddingClient(provider="none")
    assert c.embed_batch([]) == []
    assert c.embed_text("   ") is None


# ----------------------------------------------------------------------
# dimension mismatch, "none", cooldown, native attempt budget
# ----------------------------------------------------------------------

def test_dimension_mismatch_sets_last_error(monkeypatch):
    enc = _FakeEncoder(width=3)  # client expects 4
    monkeypatch.setattr(EmbeddingClient, "_get_local_embedder", lambda self: enc)
    c = EmbeddingClient(provider="local", model="fake-granite", dimensions=4)

    assert c.embed_batch(["alpha"]) == [None]
    assert "dimension mismatch" in (c.last_error or "")
    assert "EMBEDDING_DIMENSIONS" in (c.last_error or "")


def test_none_provider_gate_and_embed():
    c = EmbeddingClient(provider="none")
    assert c.availability_error() == "embeddings disabled (EMBEDDING_PROVIDER=none)"
    assert c.embed_batch(["a", "b"]) == [None, None]
    assert "EMBEDDING_PROVIDER=none" in (c.last_error or "")


def test_failure_cooldown_skips_remote_and_keeps_error(monkeypatch):
    calls = {"n": 0}

    def _boom(self):
        calls["n"] += 1
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(EmbeddingClient, "_get_openai_client", _boom)
    c = EmbeddingClient(
        provider="openai", api_key="sk-x", model="m", dimensions=2,
        failure_cooldown_seconds=60.0,
    )
    assert c.embed_batch(["a"]) == [None]
    assert calls["n"] == 1
    assert "endpoint down" in (c.last_error or "")
    # Within the cooldown: no remote attempt, last_error preserved.
    assert c.embed_batch(["a"]) == [None]
    assert calls["n"] == 1
    assert "endpoint down" in (c.last_error or "")
    # After expiry the remote path is retried.
    c._cooldown_until = 0.0
    assert c.embed_batch(["a"]) == [None]
    assert calls["n"] == 2


def test_native_attempt_budget_follows_max_retries(monkeypatch):
    import time
    import urllib.error
    import urllib.request

    seen = {"n": 0, "timeout": None}

    def _urlopen(req, timeout=None):
        seen["n"] += 1
        seen["timeout"] = timeout
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    # Hot-path shape (skills/tool search): exactly one bounded attempt.
    c = EmbeddingClient(
        provider="cohere", api_key="co-x", model="embed-v4.0",
        dimensions=4, max_retries=0, native_timeout=10.0,
    )
    assert c.embed_batch(["a"]) == [None]
    assert seen["n"] == 1
    assert seen["timeout"] == 10.0
    assert "embedding call failed" in (c.last_error or "")

    # Legacy ingest shape (memory index): the 6-attempt backoff is preserved
    # with the historical 120s per-request timeout.
    seen["n"] = 0
    c2 = EmbeddingClient(
        provider="cohere", api_key="co-x", model="embed-v4.0", dimensions=4,
    )
    assert c2.embed_batch(["a"]) == [None]
    assert seen["n"] == 6
    assert seen["timeout"] == 120.0


# ----------------------------------------------------------------------
# silent-failure hardening (short/empty/unparseable 200 responses)
# ----------------------------------------------------------------------

def test_short_openai_response_sets_last_error(monkeypatch):
    """A 200 with fewer data items than inputs must record an error, not
    degrade silently (the rebuild/search warnings key off last_error)."""
    c = EmbeddingClient(provider="openai", api_key="sk-x", model="m", dimensions=2)

    class _ShortEmbeddings:
        def create(self, input, **kwargs):
            return _FakeResponse([_FakeItem([0.5, 0.5], index=0)])  # 1 of 2

    class _ShortOpenAI:
        embeddings = _ShortEmbeddings()

        def with_options(self, **kwargs):
            return self

    monkeypatch.setattr(
        EmbeddingClient, "_get_openai_client", lambda self: _ShortOpenAI()
    )
    out = c.embed_batch(["a", "b"])
    assert out == [[0.5, 0.5], None]
    assert "missing 1 of 2" in (c.last_error or "")


def test_empty_remote_response_arms_cooldown(monkeypatch):
    """An all-None remote batch is a failure for cooldown purposes whatever
    produced it (here: an empty 200 data array)."""

    class _EmptyEmbeddings:
        def create(self, input, **kwargs):
            return _FakeResponse([])

    class _EmptyOpenAI:
        embeddings = _EmptyEmbeddings()

        def with_options(self, **kwargs):
            return self

    monkeypatch.setattr(
        EmbeddingClient, "_get_openai_client", lambda self: _EmptyOpenAI()
    )
    c = EmbeddingClient(
        provider="openai", api_key="sk-x", model="m", dimensions=2,
        failure_cooldown_seconds=60.0,
    )
    assert c.embed_batch(["a", "b"]) == [None, None]
    assert "missing 2 of 2" in (c.last_error or "")
    assert c._cooldown_until > 0  # armed


def test_native_unparseable_200_never_raises(monkeypatch):
    """A native 200 whose body is not the expected JSON (an HTML error page
    from a CDN) must return Nones with last_error set, never raise."""
    import urllib.request

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"<html>gateway error</html>"

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResp()
    )
    c = EmbeddingClient(
        provider="cohere", api_key="co-x", model="embed-v4.0",
        dimensions=4, max_retries=0, failure_cooldown_seconds=60.0,
    )
    assert c.embed_batch(["a"]) == [None]
    assert "response unusable" in (c.last_error or "")
    assert c._cooldown_until > 0  # armed


def test_native_timeout_override_reaches_native_path(monkeypatch):
    """A per-call batch timeout must reach the native providers too, not just
    the openai path (the caller's base native budget is query-shaped)."""
    import time
    import urllib.error
    import urllib.request

    seen = {"timeout": None}

    def _urlopen(req, timeout=None):
        seen["timeout"] = timeout
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    c = EmbeddingClient(
        provider="cohere", api_key="co-x", model="embed-v4.0",
        dimensions=4, max_retries=0, native_timeout=10.0,
    )
    assert c.embed_batch(["a"], timeout=60.0) == [None]
    assert seen["timeout"] == 60.0
