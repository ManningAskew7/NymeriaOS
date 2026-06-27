"""Tests for ThreadConfigManager's callable-thread scan paths and the
mtime-keyed parse cache behind them (optimization slice 05 F10).

These cover list_callable_threads / get_callable_thread_by_name: result-set
correctness, owner scoping, the legacy ``is_agent`` migration alias, cache
freshness on save/delete, the validation-skip pre-filter, short-circuit lookup,
and the cache's mutation/IO safety invariants.
"""

from __future__ import annotations

import json
from pathlib import Path

from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager


def _save(mgr: ThreadConfigManager, thread_id: str, *, callable_=False, name=None, **kw):
    mgr.save_config(
        ThreadConfig(thread_id=thread_id, callable=callable_, callable_name=name, **kw)
    )


# --- has_customizations registration ---------------------------------------


def test_has_customizations_tracks_claude_code_overrides():
    assert ThreadConfig(thread_id="t").has_customizations() is False
    assert (
        ThreadConfig(thread_id="t", claude_code_model="claude-opus-4-8").has_customizations()
        is True
    )
    assert (
        ThreadConfig(thread_id="t", claude_code_mode="plan").has_customizations() is True
    )


# --- result-set correctness ------------------------------------------------


def test_list_callable_threads_returns_only_callable(tmp_path: Path):
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "plain-1")
    _save(mgr, "plain-2")
    _save(mgr, "agent-a", callable_=True, name="AgentA")
    _save(mgr, "agent-b", callable_=True, name="AgentB")

    got = mgr.list_callable_threads()
    assert sorted(tc.callable_name for tc in got) == ["AgentA", "AgentB"]
    assert all(tc.callable for tc in got)


def test_list_callable_threads_owner_scoping(tmp_path: Path):
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "owner-helper", callable_=True, name="Helper")
    _save(mgr, "other-helper", callable_=True, name="OtherHelper")

    scoped = mgr.list_callable_threads(owned_thread_ids={"owner-helper"})
    assert [tc.callable_name for tc in scoped] == ["Helper"]

    # Empty set scopes to nothing; None returns the full list.
    assert mgr.list_callable_threads(owned_thread_ids=set()) == []
    assert len(mgr.list_callable_threads()) == 2


def test_owner_scoping_uses_unsanitized_thread_id(tmp_path: Path):
    """owned_thread_ids holds original IDs; filtering must use tc.thread_id, not
    the sanitized filename stem (a thread_id with a space would otherwise be
    silently excluded)."""
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "my agent", callable_=True, name="Spaced")

    scoped = mgr.list_callable_threads(owned_thread_ids={"my agent"})
    assert [tc.callable_name for tc in scoped] == ["Spaced"]
    assert scoped[0].thread_id == "my agent"


def test_get_callable_thread_by_name_match_and_miss(tmp_path: Path):
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "agent-a", callable_=True, name="AgentA")

    assert mgr.get_callable_thread_by_name("AgentA").thread_id == "agent-a"
    assert mgr.get_callable_thread_by_name("Nope") is None


def test_get_callable_thread_by_name_owner_scoped_collision(tmp_path: Path):
    """Two users can each own a callable named 'Helper'."""
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "owner-helper", callable_=True, name="Helper")
    _save(mgr, "bob-helper", callable_=True, name="Helper")

    owner = mgr.get_callable_thread_by_name("Helper", owned_thread_ids={"owner-helper"})
    gf = mgr.get_callable_thread_by_name("Helper", owned_thread_ids={"bob-helper"})
    assert owner.thread_id == "owner-helper"
    assert gf.thread_id == "bob-helper"


# --- robustness ------------------------------------------------------------


def test_malformed_config_is_skipped_not_raised(tmp_path: Path):
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "agent-a", callable_=True, name="AgentA")
    (tmp_path / "thread_configs" / "broken.json").write_text("{not json", encoding="utf-8")

    got = mgr.list_callable_threads()
    assert [tc.callable_name for tc in got] == ["AgentA"]


def test_callable_config_failing_validation_is_skipped(tmp_path: Path):
    """A config that parses and looks callable but fails model_validate (here an
    out-of-range callable_max_iterations) is skipped, not raised."""
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "good", callable_=True, name="Good")
    (tmp_path / "thread_configs" / "bad.json").write_text(
        json.dumps(
            {
                "thread_id": "bad",
                "callable": True,
                "callable_name": "Bad",
                "callable_max_iterations": 999999,
            }
        ),
        encoding="utf-8",
    )

    assert [tc.callable_name for tc in mgr.list_callable_threads()] == ["Good"]


def test_non_object_config_is_skipped(tmp_path: Path):
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "agent-a", callable_=True, name="AgentA")
    (tmp_path / "thread_configs" / "weird.json").write_text("[1, 2, 3]", encoding="utf-8")

    assert [tc.callable_name for tc in mgr.list_callable_threads()] == ["AgentA"]


def test_legacy_is_agent_alias_is_included(tmp_path: Path):
    """A pre-migration config on disk uses is_agent/agent_name; the before-
    validator maps these to callable/callable_name. The cost pre-filter must not
    drop it before validation."""
    mgr = ThreadConfigManager(tmp_path)
    (tmp_path / "thread_configs" / "legacy.json").write_text(
        json.dumps({"thread_id": "legacy", "is_agent": True, "agent_name": "LegacyBot"}),
        encoding="utf-8",
    )

    got = mgr.get_callable_thread_by_name("LegacyBot")
    assert got is not None
    assert got.callable is True
    assert got.thread_id == "legacy"


# --- cache freshness -------------------------------------------------------


def test_cache_reflects_save_toggle(tmp_path: Path):
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "t", callable_=True, name="Toggle")
    assert mgr.get_callable_thread_by_name("Toggle") is not None

    # warm the cache, then flip callable off and re-save.
    mgr.list_callable_threads()
    _save(mgr, "t", callable_=False)
    assert mgr.get_callable_thread_by_name("Toggle") is None

    # flip back on with a new name.
    _save(mgr, "t", callable_=True, name="Renamed")
    assert mgr.get_callable_thread_by_name("Renamed").thread_id == "t"


def test_cache_reflects_delete(tmp_path: Path):
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "gone", callable_=True, name="Gone")
    mgr.list_callable_threads()  # warm
    assert mgr.delete_config("gone") is True
    assert mgr.get_callable_thread_by_name("Gone") is None
    assert mgr.list_callable_threads() == []


def test_cache_picks_up_out_of_band_edit_via_mtime(tmp_path: Path, monkeypatch):
    """An external edit (e.g. docker exec) that bypasses save_config is still
    detected because the cache key includes mtime+size."""
    mgr = ThreadConfigManager(tmp_path)
    _save(mgr, "x", callable_=True, name="Before")
    mgr.list_callable_threads()  # warm the cache

    # Rewrite the file directly with a bumped mtime, bypassing save_config.
    path = tmp_path / "thread_configs" / "x.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["callable_name"] = "After"
    path.write_text(json.dumps(data), encoding="utf-8")
    st = path.stat()
    import os
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert mgr.get_callable_thread_by_name("After") is not None
    assert mgr.get_callable_thread_by_name("Before") is None


# --- optimization invariants ----------------------------------------------


def _spy_model_validate(monkeypatch):
    calls: list = []
    orig = ThreadConfig.model_validate

    def spy(data, *args, **kwargs):
        calls.append(data)
        return orig(data, *args, **kwargs)

    monkeypatch.setattr(ThreadConfig, "model_validate", spy)
    return calls


def test_non_callable_configs_skip_validation(tmp_path: Path, monkeypatch):
    mgr = ThreadConfigManager(tmp_path)
    for i in range(5):
        _save(mgr, f"plain-{i}")
    _save(mgr, "agent-a", callable_=True, name="AgentA")
    _save(mgr, "agent-b", callable_=True, name="AgentB")

    calls = _spy_model_validate(monkeypatch)
    mgr.list_callable_threads()
    # Only the two callable configs get fully validated; the 5 plain ones are
    # filtered out on the raw dict without a model_validate.
    assert len(calls) == 2


def test_get_by_name_short_circuits(tmp_path: Path, monkeypatch):
    mgr = ThreadConfigManager(tmp_path)
    # Stems sort lexically: aaa, bbb, ccc.
    _save(mgr, "aaa", callable_=True, name="First")
    _save(mgr, "bbb", callable_=True, name="Second")
    _save(mgr, "ccc", callable_=True, name="Third")

    calls = _spy_model_validate(monkeypatch)
    assert mgr.get_callable_thread_by_name("First").thread_id == "aaa"
    assert len(calls) == 1  # stopped after the first match

    calls.clear()
    assert mgr.get_callable_thread_by_name("Third").thread_id == "ccc"
    assert len(calls) == 3  # had to scan all three

    calls.clear()
    assert mgr.get_callable_thread_by_name("Missing") is None
    assert len(calls) == 3  # scanned all, no match


def test_cache_hit_avoids_rereading_file(tmp_path: Path, monkeypatch):
    mgr = ThreadConfigManager(tmp_path)
    for i in range(3):
        _save(mgr, f"agent-{i}", callable_=True, name=f"Agent{i}")

    import nymeria.core.thread_config as tc_mod
    loads: list = []
    orig_load = tc_mod.json.load

    def spy_load(fp, *a, **k):
        loads.append(1)
        return orig_load(fp, *a, **k)

    monkeypatch.setattr(tc_mod.json, "load", spy_load)

    mgr.list_callable_threads()  # cold: reads all 3 files
    assert len(loads) == 3
    loads.clear()
    mgr.list_callable_threads()  # warm: all unchanged -> no file reads
    assert loads == []


def test_validation_does_not_mutate_cached_dict(tmp_path: Path):
    """The model's before-validator pops is_agent / sets callable; it must run on
    a copy so the cached dict stays intact for the next call."""
    mgr = ThreadConfigManager(tmp_path)
    (tmp_path / "thread_configs" / "legacy.json").write_text(
        json.dumps({"thread_id": "legacy", "is_agent": True, "agent_name": "LegacyBot"}),
        encoding="utf-8",
    )
    mgr.list_callable_threads()  # validates + caches

    cached = mgr._callable_scan_cache["legacy.json"][1]
    assert "is_agent" in cached and "callable" not in cached
    assert "agent_name" in cached and "callable_name" not in cached
    # A second call still resolves correctly off the unmutated cached dict.
    assert mgr.get_callable_thread_by_name("LegacyBot") is not None
    assert mgr.list_callable_threads()[0].thread_id == "legacy"
