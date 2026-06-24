from __future__ import annotations

import pytest

from nymeria.core.generated_image_context import NATIVE_IMAGE_ARTIFACT_KEY
from nymeria.tools import image_gen_integrations as igi

_CONFIG = {"configurable": {"user_id": "owner@example.com"}}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    """Minimal stand-in for httpx.Client used by the async provider tools."""

    def __init__(self, *, post=None, gets=None, get_default=None):
        self._post = post or {}
        self._gets = list(gets or [])
        self._get_default = get_default
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.calls.append(("post", url, json))
        return FakeResponse(self._post)

    def get(self, url, headers=None):
        self.calls.append(("get", url))
        if self._gets:
            return FakeResponse(self._gets.pop(0))
        if self._get_default is not None:
            return FakeResponse(self._get_default)
        return FakeResponse({})


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    return tmp_path


def _assert_generated(content, artifact, *, provider, model):
    metadata = artifact[NATIVE_IMAGE_ARTIFACT_KEY]
    assert "[attach:" in content
    assert f"Generated image via {provider} ({model})" in content
    assert metadata["provider"] == provider
    assert metadata["model"] == model
    assert metadata["native_context_enabled"] is True


# --- OpenAI / Gemini (reuse the byte producers) ------------------------------
def test_image_gen_openai_end_to_end(workspace, monkeypatch):
    monkeypatch.setattr(igi, "_get_openai_image_api_key", lambda config=None: "k")
    monkeypatch.setattr(
        igi, "_generate_openai",
        lambda prompt, cfg, api_key=None: (b"openai-bytes", "image/png", "gpt-image-2"),
    )

    content, artifact = igi.image_gen_openai.func(prompt="a cat", config=_CONFIG)

    _assert_generated(content, artifact, provider="openai", model="gpt-image-2")
    files = list((workspace / "images" / "generated" / "owner-example.com").glob("*.png"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"openai-bytes"


def test_image_gen_gemini_end_to_end(workspace, monkeypatch):
    monkeypatch.setattr(igi, "_get_gemini_image_api_key", lambda config=None: "k")
    monkeypatch.setattr(
        igi, "_generate_gemini",
        lambda prompt, cfg, api_key=None: (b"gemini-bytes", "image/png", "gemini-3-pro-image-preview"),
    )

    content, artifact = igi.image_gen_gemini.func(prompt="a banana", config=_CONFIG)

    _assert_generated(content, artifact, provider="gemini", model="gemini-3-pro-image-preview")


# --- FLUX (async submit + poll) ----------------------------------------------
def test_image_gen_flux_polls_then_downloads(workspace, monkeypatch):
    import httpx

    monkeypatch.setattr(igi, "_get_bfl_api_key", lambda config=None: "k")
    fake = FakeClient(
        post={"id": "job1", "polling_url": "https://poll.example/job1"},
        gets=[{"status": "Ready", "result": {"sample": "https://img.example/out.png"}}],
    )
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    monkeypatch.setattr(igi, "download_image_bytes", lambda url, **k: (b"flux-bytes", "image/png"))

    content, artifact = igi.image_gen_flux.func(prompt="a fox", config=_CONFIG)

    _assert_generated(content, artifact, provider="bfl", model="flux-2-pro")
    assert ("post", "https://api.bfl.ai/v1/flux-2-pro", {"prompt": "a fox", "aspect_ratio": "1:1", "output_format": "png"}) in fake.calls


def test_image_gen_flux_times_out(workspace, monkeypatch):
    import httpx

    monkeypatch.setattr(igi, "_get_bfl_api_key", lambda config=None: "k")
    monkeypatch.setattr(igi.time, "sleep", lambda *a, **k: None)
    fake = FakeClient(post={"id": "job1", "polling_url": "https://poll"}, get_default={"status": "Pending"})
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)

    content, artifact = igi.image_gen_flux.func(prompt="slow", config=_CONFIG)

    assert content.startswith("[Error]")
    assert "timed out" in content
    assert artifact == {}


# --- Replicate (Prefer: wait, with poll fallback) ----------------------------
def test_image_gen_replicate_prefer_wait(workspace, monkeypatch):
    import httpx

    monkeypatch.setattr(igi, "_get_replicate_api_key", lambda config=None: "k")
    fake = FakeClient(post={"status": "succeeded", "output": ["https://img.example/out.png"]})
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    monkeypatch.setattr(igi, "download_image_bytes", lambda url, **k: (b"rep-bytes", "image/png"))

    content, artifact = igi.image_gen_replicate.func(
        prompt="a dog", model="z-image-turbo", config=_CONFIG
    )

    _assert_generated(content, artifact, provider="replicate", model="z-image-turbo")


def test_image_gen_replicate_polls_when_not_ready(workspace, monkeypatch):
    import httpx

    monkeypatch.setattr(igi, "_get_replicate_api_key", lambda config=None: "k")
    monkeypatch.setattr(igi.time, "sleep", lambda *a, **k: None)
    fake = FakeClient(
        post={"status": "processing", "urls": {"get": "https://poll.example/p1"}},
        gets=[{"status": "succeeded", "output": "https://img.example/out.png"}],
    )
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    monkeypatch.setattr(igi, "download_image_bytes", lambda url, **k: (b"rep-bytes", "image/png"))

    content, artifact = igi.image_gen_replicate.func(prompt="a dog", config=_CONFIG)

    _assert_generated(content, artifact, provider="replicate", model="flux-schnell")


# --- fal.ai (synchronous endpoint) -------------------------------------------
def test_image_gen_fal_end_to_end(workspace, monkeypatch):
    import httpx

    monkeypatch.setattr(igi, "_get_fal_api_key", lambda config=None: "k")
    fake = FakeClient(post={"images": [{"url": "https://img.example/out.png"}]})
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    monkeypatch.setattr(igi, "download_image_bytes", lambda url, **k: (b"fal-bytes", "image/png"))

    content, artifact = igi.image_gen_fal.func(prompt="a bird", config=_CONFIG)

    _assert_generated(content, artifact, provider="fal", model="z-image-turbo")


# --- Credential resolution ---------------------------------------------------
def test_missing_key_returns_setup_hint(monkeypatch):
    monkeypatch.setattr(igi, "_get_openai_image_api_key", lambda config=None: None)

    content, artifact = igi.image_gen_openai.func(prompt="x")

    assert content.startswith("[Error]")
    assert "request_credential" in content
    assert "native_tool:image_gen_openai" in content
    assert artifact == {}


def test_key_prefers_vault_over_settings(monkeypatch):
    import nymeria.tools.native_credentials as nc

    class FakeCred:
        value = "vault-key"
        credential_id = "c1"
        field_name = "api_key"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: FakeCred())

    assert igi._get_bfl_api_key(config=None) == "vault-key"


def test_key_falls_back_to_settings(monkeypatch):
    import nymeria.config as config
    import nymeria.tools.native_credentials as nc

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)

    class FakeSettings:
        bfl_api_key = "settings-key"

    monkeypatch.setattr(config, "get_settings", lambda: FakeSettings())

    assert igi._get_bfl_api_key(config=None) == "settings-key"


# --- Registration / metadata -------------------------------------------------
def test_image_gen_tools_registered_as_optional_image_tools():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    names = [
        "image_gen_openai",
        "image_gen_gemini",
        "image_gen_flux",
        "image_gen_replicate",
        "image_gen_fal",
    ]
    registered = {t.name for t in igi.IMAGE_GEN_INTEGRATION_TOOLS}
    for name in names:
        assert name in CATALOG_TOOLS
        assert name in registered
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.IMAGE
        assert metadata.security_level == SecurityLevel.MODERATE
        assert metadata.default_enabled is False


def test_image_generate_tool_retired():
    from nymeria.tools import CATALOG_TOOLS

    assert "image_generate" not in CATALOG_TOOLS


# --- Shared _image_gen_tool envelope decorator -------------------------------
def test_image_gen_tool_passes_through_body_result():
    @igi._image_gen_tool
    def body(prompt, *args, **kwargs):
        return f"ok:{prompt}", {"args": args, "kwargs": kwargs}

    # The decorator strips the prompt and forwards positional + keyword args
    # verbatim to the wrapped body.
    assert body("  hello  ", "pos", config=None) == (
        "ok:hello",
        {"args": ("pos",), "kwargs": {"config": None}},
    )


def test_image_gen_tool_empty_prompt_guard_short_circuits():
    calls = []

    @igi._image_gen_tool
    def body(prompt, **kwargs):
        calls.append(prompt)
        return "should not run", {}

    assert body("   ", config=None) == ("[Error]: prompt is required.", {})
    assert body(None, config=None) == ("[Error]: prompt is required.", {})
    # The wrapped body must never run when the prompt is empty/whitespace.
    assert calls == []


def test_image_gen_tool_failsoft_envelope_uses_function_name(caplog):
    import logging

    @igi._image_gen_tool
    def image_gen_demo(prompt, **kwargs):
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR, logger=igi.logger.name):
        content, artifact = image_gen_demo("x", config=None)

    assert content == "[Error]: image_gen_demo failed: kaboom"
    assert artifact == {}
    # logger.exception renders "<name> failed" at ERROR with the traceback.
    assert "image_gen_demo failed" in caplog.text


_TOOL_NAMES = [
    "image_gen_openai",
    "image_gen_gemini",
    "image_gen_flux",
    "image_gen_replicate",
    "image_gen_fal",
]

_EXPECTED_PROPS = {
    "image_gen_openai": {"prompt", "size", "quality", "output_format", "model", "output_name"},
    "image_gen_gemini": {"prompt", "aspect_ratio", "image_size", "model", "output_name"},
    "image_gen_flux": {"prompt", "aspect_ratio", "model", "seed", "output_name"},
    "image_gen_replicate": {"prompt", "aspect_ratio", "model", "seed", "output_name"},
    "image_gen_fal": {"prompt", "image_size", "model", "seed", "output_name"},
}


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_empty_prompt_guard_wired_into_every_tool(tool_name):
    # An empty prompt must short-circuit before any credential resolution, so
    # this needs no key monkeypatch; a fall-through would surface a *different*
    # missing-key error instead.
    tool = getattr(igi, tool_name)
    content, artifact = tool.func(prompt="   ", config=_CONFIG)
    assert content == "[Error]: prompt is required."
    assert artifact == {}


@pytest.mark.parametrize("tool_name", _TOOL_NAMES)
def test_tool_schema_unchanged_by_decorator(tool_name):
    # Stacking the functools.wraps decorator under @tool must leave the
    # agent-facing contract (name, args schema, hidden InjectedToolArg, response
    # format) byte-for-shape identical.
    tool = getattr(igi, tool_name)
    schema = tool.args_schema.model_json_schema()
    assert set(schema["properties"]) == _EXPECTED_PROPS[tool_name]
    assert schema["required"] == ["prompt"]
    assert "config" not in schema["properties"]
    assert tool.name == tool_name
    assert tool.response_format == "content_and_artifact"


def test_failsoft_envelope_wired_into_synchronous_producer_path(monkeypatch):
    # The flux timeout test covers the poll path; this covers the
    # synchronous-producer path end to end through openai (gemini shares it).
    monkeypatch.setattr(igi, "_get_openai_image_api_key", lambda config=None: "k")

    def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(igi, "_generate_openai", boom)

    content, artifact = igi.image_gen_openai.func(prompt="a cat", config=_CONFIG)

    assert content == "[Error]: image_gen_openai failed: provider exploded"
    assert artifact == {}
