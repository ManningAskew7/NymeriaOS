"""Integration tests for file_read's extraction_prompt path."""

from __future__ import annotations

import pytest
from PIL import Image

from nymeria.tools import filesystem, llm_extract
from nymeria.tools.filesystem import file_read


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NYMERIA_WORKSPACE_DIR", str(tmp_path))
    yield


def test_extraction_prompt_runs_secondary_model_and_tags(tmp_path, monkeypatch):
    path = tmp_path / "big.log"
    path.write_text("line one\nline two\n", encoding="utf-8")

    calls = {}

    def fake_extraction(content, prompt):
        calls["content"] = content
        calls["prompt"] = prompt
        return "EXTRACTED", "fake-model"

    monkeypatch.setattr(llm_extract, "run_extraction", fake_extraction)

    content, artifact = file_read.func(str(path), extraction_prompt="the second line")
    assert content == "EXTRACTED\n\n[Extracted by fake-model]"
    assert artifact == {}
    assert calls["prompt"] == "the second line"
    assert "line two" in calls["content"]   # the full file text is handed to the LLM


def test_empty_extraction_prompt_returns_raw_and_skips_llm(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("alpha beta\n", encoding="utf-8")

    def boom(content, prompt):
        raise AssertionError("run_extraction must not run when extraction_prompt is empty")

    monkeypatch.setattr(llm_extract, "run_extraction", boom)

    content, artifact = file_read.func(str(path))
    assert content.strip() == "alpha beta"
    assert artifact == {}


def test_extraction_error_returned_without_attribution(tmp_path, monkeypatch):
    path = tmp_path / "data.txt"
    path.write_text("payload\n", encoding="utf-8")

    monkeypatch.setattr(
        llm_extract,
        "run_extraction",
        lambda content, prompt: ("[Error]: Extraction step failed: X", ""),
    )

    content, artifact = file_read.func(str(path), extraction_prompt="anything")
    assert content.startswith("[Error]: Extraction step failed")
    assert "Extracted by" not in content


def test_extraction_prompt_ignored_for_images(tmp_path, monkeypatch):
    # Images go through the vision path and return before the text branch, so the
    # extraction step is never reached.
    path = tmp_path / "pic.png"
    Image.new("RGB", (8, 8), "red").save(path, format="PNG")

    # Force the no-active-agent note path deterministically (independent of any
    # agent singleton state) and assert extraction is never invoked.
    monkeypatch.setattr(filesystem, "_resolve_active_llm_config", lambda _config: None)

    def boom(content, prompt):
        raise AssertionError("run_extraction must not run for an image read")

    monkeypatch.setattr(llm_extract, "run_extraction", boom)

    content, artifact = file_read.func(str(path), extraction_prompt="describe it")
    assert "could not show it to you" in content  # the no_active_agent [Note]
    assert artifact == {}
