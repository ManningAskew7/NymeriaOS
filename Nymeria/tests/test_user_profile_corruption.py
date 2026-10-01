"""A user profile that does not load is preserved, never overwritten (#400).

Before: any load error (one invalid memory, a UTF-8 BOM, truncated JSON, an
unreadable file) made ``get_profile`` build a fresh profile that the seeding
migrations then SAVED, so a plain read, which every turn's prompt build does,
erased the user's memories, preferences, personality, opt-ins and skills.

Now a profile that parses but fails validation is salvaged field by field and
memory by memory, the original bytes are copied to ``quarantine/`` beside it,
and the salvage atomically replaces the file; undecodable bytes are preserved
the same way; an unreadable or unrepairable file is never written, by
``atomic_update`` or by a direct ``save_profile`` of the stand-in.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nymeria.core import user_profile as user_profile_module
from nymeria.core.user_profile import ProfileUnavailableError, UserProfileManager

USER = "alice"


@pytest.fixture
def recorders(monkeypatch):
    """Capture the audit row and the (off-thread) owner alert."""
    audits: list[tuple[str, str, str]] = []
    alerts: list[str] = []
    alerted = threading.Event()

    def _audit(store: str, detail: str, *, user_id: str = "default") -> None:
        audits.append((store, detail, user_id))

    def _alert(message: str, settings: Any, *, user_id: str, **_: Any) -> None:
        alerts.append(message)
        alerted.set()

    monkeypatch.setattr("nymeria.core.activity_log.log_external_edit", _audit)
    monkeypatch.setattr("nymeria.core.notification_dispatch.send_owner_alert", _alert)
    return SimpleNamespace(audits=audits, alerts=alerts, alerted=alerted)


def _seed(manager: UserProfileManager, user_id: str = USER) -> None:
    with manager.atomic_update(user_id) as profile:
        profile.add_memory("favorite_color", "blue")
        profile.add_memory("pet", "a cat called Miso")
        profile.add_memory("coffee", "flat white")
        profile.set_notification_preference("default_profile", "phone")
        profile.personality_overrides = {"tone": "dry"}
    # Settle the one-shot migrations (the RAG watermark fires on the first
    # reload after creation and takes the lock to save), so the file on disk
    # is what every later healthy read returns unchanged.
    manager.get_profile(user_id)


def _path(manager: UserProfileManager, user_id: str = USER) -> Path:
    return manager.users_dir / user_id / "profile.json"


def _rewrite(manager: UserProfileManager, mutate) -> str:
    path = _path(manager)
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    text = json.dumps(data, indent=2)
    path.write_text(text, encoding="utf-8")
    return text


def _break_memory(key: str):
    def _mutate(data: dict) -> None:
        for raw in data["memories"]:
            if raw["key"] == key:
                raw["access_count"] = -1

    return _mutate


def _quarantined(manager: UserProfileManager, user_id: str = USER) -> list[Path]:
    return sorted((manager.users_dir / user_id / "quarantine").glob("profile.corrupt-*"))


def _on_disk(manager: UserProfileManager, user_id: str = USER) -> dict:
    return json.loads(_path(manager, user_id).read_text(encoding="utf-8"))


def _memory_keys(profile_or_data) -> set[str]:
    memories = (
        profile_or_data["memories"]
        if isinstance(profile_or_data, dict)
        else [m.model_dump() for m in profile_or_data.memories]
    )
    return {m["key"] for m in memories}


# --- the headline: a plain read never loses a valid memory ------------------


def test_one_invalid_memory_keeps_everything_else(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    corrupt_text = _rewrite(manager, _break_memory("pet"))

    profile = manager.get_profile(USER)

    assert _memory_keys(profile) == {"favorite_color", "coffee"}
    assert profile.get_notification_preferences()["default_profile"] == "phone"
    assert profile.personality_overrides == {"tone": "dry"}
    quarantined = _quarantined(manager)
    assert [q.read_text(encoding="utf-8") for q in quarantined] == [corrupt_text]
    on_disk = _on_disk(manager)
    assert _memory_keys(on_disk) == {"favorite_color", "coffee"}
    assert on_disk["preferences"]["notifications"]["default_profile"] == "phone"
    assert len(recorders.audits) == 1
    store, detail, audited_user = recorders.audits[0]
    assert (store, audited_user) == ("profile", USER)
    assert "kept 2 memories" in detail and "dropped 1" in detail
    assert recorders.alerted.wait(5)
    assert len(recorders.alerts) == 1
    assert recorders.alerts[0].startswith("[PROFILE REPAIRED]")
    assert "2 memories were kept; 1 memory was unreadable and dropped" in recorders.alerts[0]
    assert f"users/{USER}/quarantine/{quarantined[0].name}" in recorders.alerts[0]


def test_one_invalid_field_resets_only_that_field(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    _rewrite(manager, lambda data: data.update(personality_overrides={"tone": 42}))

    profile = manager.get_profile(USER)

    assert profile.personality_overrides == {}
    assert _memory_keys(profile) == {"favorite_color", "pet", "coffee"}
    assert profile.get_notification_preferences()["default_profile"] == "phone"
    assert _memory_keys(_on_disk(manager)) == {"favorite_color", "pet", "coffee"}
    assert recorders.alerted.wait(5)
    assert "safe default: personality_overrides.tone." in recorders.alerts[0]
    assert "unreadable and dropped" not in recorders.alerts[0]


def test_a_second_load_after_a_repair_is_clean(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    _rewrite(manager, _break_memory("pet"))
    manager.get_profile(USER)
    assert recorders.alerted.wait(5)

    again = manager.get_profile(USER)

    assert _memory_keys(again) == {"favorite_color", "coffee"}
    assert len(_quarantined(manager)) == 1
    assert len(recorders.audits) == 1
    assert len(recorders.alerts) == 1


def test_an_update_on_a_corrupt_profile_builds_on_the_salvage(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    _rewrite(manager, _break_memory("pet"))

    with manager.atomic_update(USER) as profile:
        profile.add_memory("city", "Sydney")

    assert _memory_keys(_on_disk(manager)) == {"favorite_color", "coffee", "city"}


# --- undecodable bytes -------------------------------------------------------


@pytest.mark.parametrize(
    "encode",
    [
        lambda text: text[: len(text) // 2].encode("utf-8"),
        lambda text: text.replace("\\u00e9", "\u00e9").encode("cp1252"),
        lambda text: b"",
        lambda text: b"[]",
    ],
    ids=["truncated", "cp1252", "empty", "not-an-object"],
)
def test_an_undecodable_profile_is_preserved_and_a_fresh_one_starts(
    tmp_path, recorders, encode
):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    with manager.atomic_update(USER) as profile:
        profile.add_memory("drink", "caf\u00e9 au lait")
    path = _path(manager)
    corrupt = encode(path.read_text(encoding="utf-8"))
    path.write_bytes(corrupt)

    profile = manager.get_profile(USER)

    assert profile.memories == []
    # A fresh profile is seeded and saved exactly like a first creation.
    assert profile.tool_preferences.default_thread_tools is not None
    assert _on_disk(manager)["memories"] == []
    assert [q.read_bytes() for q in _quarantined(manager)] == [corrupt]
    assert recorders.alerted.wait(5)
    assert recorders.alerts[0].startswith("[PROFILE CORRUPT]")
    assert "kept" not in recorders.audits[0][1]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_a_utf16_or_utf32_profile_loads_untouched(tmp_path, recorders, encoding):
    """What PowerShell 5.1's redirect and Out-File write: valid JSON text."""
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    path = _path(manager)
    path.write_bytes(path.read_text(encoding="utf-8").encode(encoding))

    profile = manager.get_profile(USER)

    assert _memory_keys(profile) == {"favorite_color", "pet", "coffee"}
    assert not _quarantined(manager)
    assert recorders.audits == []


# --- salvage is as fine-grained as the damage --------------------------------


def test_one_bad_opt_in_flag_costs_only_itself(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    with manager.atomic_update(USER) as profile:
        profile.opt_in.rag_enabled = False
        profile.opt_in.personality_adaptation = True

    def _bad_flag(data: dict) -> None:
        data["opt_in"]["memory_enabled"] = {"not": "a bool"}

    _rewrite(manager, _bad_flag)

    profile = manager.get_profile(USER)

    assert profile.opt_in.rag_enabled is False, "a user's RAG opt-out was undone"
    assert profile.opt_in.personality_adaptation is True
    assert profile.opt_in.memory_enabled is None
    assert _on_disk(manager)["opt_in"]["rag_enabled"] is False
    assert recorders.alerted.wait(5)
    assert "opt_in.memory_enabled" in recorders.alerts[0]


def test_an_unreadable_rag_flag_falls_to_off_not_the_new_user_default(
    tmp_path, recorders
):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    with manager.atomic_update(USER) as profile:
        profile.opt_in.rag_enabled = False
    _rewrite(manager, lambda data: data["opt_in"].update(rag_enabled={"x": 1}))

    profile = manager.get_profile(USER)

    assert profile.opt_in.rag_enabled is False
    assert _on_disk(manager)["opt_in"]["rag_enabled"] is False


def test_one_bad_skill_name_costs_only_itself(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    skills = manager.get_profile(USER).enabled_global_skills
    assert len(skills) >= 2
    _rewrite(manager, lambda data: data["enabled_global_skills"].insert(1, 42))

    profile = manager.get_profile(USER)

    assert profile.enabled_global_skills == skills
    assert recorders.alerted.wait(5)
    assert "enabled_global_skills[1]" in recorders.alerts[0]


def test_a_skill_list_dropped_whole_gets_its_defaults_back(tmp_path, recorders):
    from nymeria.core.user_profile import DEFAULT_GLOBAL_SKILLS

    manager = UserProfileManager(tmp_path)
    _seed(manager)
    _rewrite(manager, lambda data: data.update(enabled_global_skills="not a list"))

    profile = manager.get_profile(USER)

    assert set(DEFAULT_GLOBAL_SKILLS) <= set(profile.enabled_global_skills)
    assert set(DEFAULT_GLOBAL_SKILLS) <= set(_on_disk(manager)["enabled_global_skills"])


def test_one_bad_tool_config_keeps_the_tool_list(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    with manager.atomic_update(USER) as profile:
        profile.tool_preferences.default_thread_tools = ["memory_add", "web_search_ddgs"]
        profile.tool_preferences.custom_descriptions = {"memory_add": "mine"}
        profile.tool_preferences.tool_configs = {"memory_add": {"x": 1}}
    _rewrite(
        manager,
        lambda data: data["tool_preferences"]["tool_configs"].update(bad="not a dict"),
    )

    profile = manager.get_profile(USER)

    assert profile.tool_preferences.default_thread_tools == ["memory_add", "web_search_ddgs"]
    assert profile.tool_preferences.custom_descriptions == {"memory_add": "mine"}
    assert profile.tool_preferences.tool_configs == {"memory_add": {"x": 1}}


def test_the_repair_log_never_quotes_memory_text(tmp_path, recorders, caplog):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    with manager.atomic_update(USER) as profile:
        profile.add_memory("diagnosis", "PRIVATE-MEDICAL-DETAIL")

    def _break_private(data: dict) -> None:
        for raw in data["memories"]:
            if raw["key"] == "diagnosis":
                raw["access_count"] = "PRIVATE-MEDICAL-DETAIL"

    _rewrite(manager, _break_private)
    caplog.set_level("DEBUG")

    manager.get_profile(USER)

    assert "PRIVATE-MEDICAL-DETAIL" not in caplog.text
    assert "memories.3.access_count" in caplog.text


# --- a BOM is not corruption -------------------------------------------------


def test_a_bom_profile_loads_fully(tmp_path, recorders):
    from nymeria.doctor import _check_web_search

    manager = UserProfileManager(tmp_path)
    _seed(manager, "default")
    path = _path(manager, "default")
    with manager.atomic_update("default") as profile:
        profile.tool_preferences.default_thread_tools = ["web_search_perplexity"]
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    profile = manager.get_profile("default")

    assert _memory_keys(profile) == {"favorite_color", "pet", "coffee"}
    assert not _quarantined(manager, "default")
    assert recorders.audits == [] and recorders.alerts == []
    # Not the fresh-install defaults the doctor falls back to on a bad read.
    check = _check_web_search(SimpleNamespace(data_dir=tmp_path))
    assert check.detail == "web_search_perplexity"


# --- unreadable or unrepairable: never written --------------------------------


def _deny_reads(monkeypatch, path: Path) -> dict:
    """Make ``path`` unreadable; flip the returned ``on`` flag to restore it
    (``monkeypatch.undo()`` would also undo the alert recorders)."""
    real_read_bytes = Path.read_bytes
    state = {"on": True}

    def _denied(self: Path) -> bytes:
        if state["on"] and self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _denied)
    return state


def test_an_unreadable_profile_is_never_written(tmp_path, recorders, monkeypatch):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    path = _path(manager)
    before = path.read_bytes()
    denied = _deny_reads(monkeypatch, path)

    profile, authoritative = manager.load_profile(USER)

    assert authoritative is False
    assert profile.memories == []
    # The stand-in still carries seeded defaults so a turn can build tools,
    # but never opts the user into conversation indexing.
    assert profile.tool_preferences.default_thread_tools
    assert profile.opt_in.rag_enabled is False
    with pytest.raises(ProfileUnavailableError, match="could not be read"):
        with manager.atomic_update(USER) as locked:
            locked.add_memory("city", "Sydney")
    # The read-modify-write routes that bypass atomic_update save the
    # stand-in directly; that must fail closed too.
    profile.add_memory("city", "Sydney")
    assert manager.save_profile(profile) is False

    denied["on"] = False
    assert path.read_bytes() == before
    assert not _quarantined(manager)
    assert recorders.audits == []


def test_a_failed_preservation_copy_refuses_writes(tmp_path, recorders, monkeypatch):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    corrupt_text = _rewrite(manager, _break_memory("pet"))
    monkeypatch.setattr(user_profile_module, "quarantine_copy", lambda *_a: None)

    profile, authoritative = manager.load_profile(USER)

    assert _memory_keys(profile) == {"favorite_color", "coffee"}
    assert authoritative is False
    with pytest.raises(ProfileUnavailableError):
        with manager.atomic_update(USER):
            pass
    assert manager.save_profile(profile) is False
    assert _path(manager).read_text(encoding="utf-8") == corrupt_text
    assert recorders.audits == []


def test_a_failed_write_back_leaves_the_original_live(
    tmp_path, recorders, monkeypatch
):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    corrupt_text = _rewrite(manager, _break_memory("pet"))
    monkeypatch.setattr(manager, "save_profile", lambda _profile: False)

    profile, authoritative = manager.load_profile(USER)

    assert _memory_keys(profile) == {"favorite_color", "coffee"}
    assert authoritative is False
    assert _path(manager).read_text(encoding="utf-8") == corrupt_text
    assert not _quarantined(manager)
    # No "repaired" claim: the owner hears the profile is unusable instead.
    assert recorders.audits == []
    assert recorders.alerted.wait(5)
    assert [a[:20] for a in recorders.alerts] == ["[PROFILE UNREADABLE]"]
    assert "could not be written back" in recorders.alerts[0]


def test_a_write_from_another_process_during_repair_is_kept(
    tmp_path, recorders, monkeypatch
):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    healthy = _path(manager).read_text(encoding="utf-8")
    corrupt_text = _rewrite(manager, _break_memory("pet"))
    real_copy = user_profile_module.quarantine_copy

    def _copy_then_other_process_writes(target: Path, data: bytes):
        result = real_copy(target, data)
        target.write_text(healthy, encoding="utf-8")
        return result

    monkeypatch.setattr(
        user_profile_module, "quarantine_copy", _copy_then_other_process_writes
    )

    profile, authoritative = manager.load_profile(USER)

    assert _memory_keys(profile) == {"favorite_color", "pet", "coffee"}
    assert authoritative is True
    assert _path(manager).read_text(encoding="utf-8") == healthy
    assert [q.read_text(encoding="utf-8") for q in _quarantined(manager)] == [corrupt_text]
    assert recorders.audits == []


def test_the_profile_file_is_present_throughout_a_repair(
    tmp_path, recorders, monkeypatch
):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    _rewrite(manager, _break_memory("pet"))
    path = _path(manager)
    seen: list[bool] = []
    real_copy = user_profile_module.quarantine_copy

    def _copy(target: Path, data: bytes):
        result = real_copy(target, data)
        seen.append(path.exists())
        return result

    monkeypatch.setattr(user_profile_module, "quarantine_copy", _copy)

    manager.get_profile(USER)

    assert seen == [True]
    assert _memory_keys(_on_disk(manager)) == {"favorite_color", "coffee"}


def test_the_startup_tool_sweep_skips_a_read_only_profile(
    tmp_path, recorders, monkeypatch
):
    """A salvaged read-only view whose tools the sweep WOULD normalize (a
    legacy ``web_search``) is skipped, not handed to save_profile."""
    from nymeria.core.agent import NymeriaAgent

    manager = UserProfileManager(tmp_path)
    _seed(manager)

    def _legacy_tools_and_a_bad_memory(data: dict) -> None:
        _break_memory("pet")(data)
        data["tool_preferences"]["default_thread_tools"] = ["web_search"]

    corrupt_text = _rewrite(manager, _legacy_tools_and_a_bad_memory)
    monkeypatch.setattr(user_profile_module, "quarantine_copy", lambda *_a: None)
    saves: list = []
    real_save = manager.save_profile
    monkeypatch.setattr(
        manager, "save_profile", lambda profile: saves.append(profile) or real_save(profile)
    )

    NymeriaAgent._migrate_tool_preferences(SimpleNamespace(profile_manager=manager))  # type: ignore[arg-type]

    assert saves == []
    assert _path(manager).read_text(encoding="utf-8") == corrupt_text


def test_the_startup_tool_sweep_leaves_an_unreadable_profile_alone(
    tmp_path, recorders, monkeypatch
):
    from nymeria.core.agent import NymeriaAgent

    manager = UserProfileManager(tmp_path)
    _seed(manager)
    path = _path(manager)
    before = path.read_bytes()
    denied = _deny_reads(monkeypatch, path)

    NymeriaAgent._migrate_tool_preferences(SimpleNamespace(profile_manager=manager))  # type: ignore[arg-type]

    denied["on"] = False
    assert path.read_bytes() == before


def test_an_unavailable_profile_alerts_once_per_episode(
    tmp_path, recorders, monkeypatch, caplog
):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    path = _path(manager)
    denied = _deny_reads(monkeypatch, path)
    caplog.set_level("DEBUG", logger="nymeria.core.user_profile")

    for _ in range(3):
        manager.get_profile(USER)

    assert recorders.alerted.wait(5)
    assert len(recorders.alerts) == 1
    assert recorders.alerts[0].startswith("[PROFILE UNREADABLE]")
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1

    denied["on"] = False
    assert manager.load_profile(USER)[1] is True
    recorders.alerted.clear()
    denied["on"] = True
    manager.get_profile(USER)
    assert recorders.alerted.wait(5)
    assert len(recorders.alerts) == 2


def test_forget_everything_also_purges_preserved_copies(
    tmp_path, recorders, monkeypatch
):
    from nymeria.tools import memory as memory_tools

    manager = UserProfileManager(tmp_path)
    _seed(manager)
    _rewrite(manager, _break_memory("pet"))
    manager.get_profile(USER)
    assert len(_quarantined(manager)) == 1
    monkeypatch.setattr(memory_tools, "_get_profile_manager", lambda: manager)
    monkeypatch.setattr(memory_tools, "_get_memory_index", lambda _user: None)
    clear_all: Any = memory_tools.memory_clear_all

    result = clear_all.func(config={"configurable": {"user_id": USER}})

    assert "1 preserved profile copy" in result
    assert not _quarantined(manager)
    assert manager.get_profile(USER).memories == []


# --- a list naming another user stays bound to its own file ------------------


def test_a_profile_claiming_another_user_saves_to_its_own_file(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)
    _seed(manager, "victim")
    victim_before = _path(manager, "victim").read_bytes()
    _seed(manager)
    _rewrite(manager, lambda data: data.update(user_id="victim"))

    assert manager.get_profile(USER).user_id == USER
    with manager.atomic_update(USER) as profile:
        profile.add_memory("city", "Sydney")

    assert _path(manager, "victim").read_bytes() == victim_before
    assert "city" in _memory_keys(_on_disk(manager))


# --- locking -----------------------------------------------------------------


def _hold_lock_in_other_thread(lock) -> tuple[threading.Event, threading.Thread]:
    held = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(5)
    return release, holder


def test_a_healthy_read_does_not_wait_on_the_user_lock(tmp_path):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    release, holder = _hold_lock_in_other_thread(manager._get_lock(USER))
    result: list = []
    reader = threading.Thread(
        target=lambda: result.append(manager.get_profile(USER)), daemon=True
    )
    try:
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), "a healthy read blocked on the user lock"
        assert _memory_keys(result[0]) == {"favorite_color", "pet", "coffee"}
    finally:
        release.set()
        holder.join(5)


def test_the_repair_rereads_under_the_lock(tmp_path, recorders, monkeypatch):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    healthy = _path(manager).read_text(encoding="utf-8")
    _rewrite(manager, _break_memory("pet"))
    repairs: list[str] = []
    real_repair = manager._repair_unloadable

    def _spy(user_id: str):
        repairs.append(user_id)
        return real_repair(user_id)

    monkeypatch.setattr(manager, "_repair_unloadable", _spy)
    result: list = []
    reader = threading.Thread(
        target=lambda: result.append(manager.get_profile(USER)), daemon=True
    )
    with manager._get_lock(USER):
        reader.start()
        reader.join(0.5)
        assert reader.is_alive(), "the repair should wait for the user lock"
        _path(manager).write_text(healthy, encoding="utf-8")
    reader.join(5)

    assert repairs == [USER]
    assert _memory_keys(result[0]) == {"favorite_color", "pet", "coffee"}
    assert not _quarantined(manager)
    assert recorders.audits == []


def test_a_busy_lock_serves_a_read_only_view_without_blocking(
    tmp_path, recorders, monkeypatch
):
    """The busy-lock view is built with the seeding migrations run in memory
    only; those must not wait on the (busy) lock either."""
    manager = UserProfileManager(tmp_path)
    _seed(manager)

    def _break_memory_and_a_watermark(data: dict) -> None:
        _break_memory("pet")(data)
        # Dropped by the salvage, so its one-shot migration fires on the view.
        data["global_skill_defaults_migrated"] = {"not": "a bool"}

    corrupt_text = _rewrite(manager, _break_memory_and_a_watermark)
    monkeypatch.setattr(user_profile_module, "_REPAIR_LOCK_TIMEOUT_SECONDS", 0.2)
    release, holder = _hold_lock_in_other_thread(manager._get_lock(USER))
    result: list = []
    reader = threading.Thread(
        target=lambda: result.append(manager.load_profile(USER)), daemon=True
    )
    try:
        reader.start()
        reader.join(3)
        assert not reader.is_alive(), "the read-only view blocked on the busy lock"
    finally:
        release.set()
        holder.join(5)

    profile, authoritative = result[0]
    assert _memory_keys(profile) == {"favorite_color", "coffee"}
    assert authoritative is False
    assert _path(manager).read_text(encoding="utf-8") == corrupt_text
    assert not _quarantined(manager)


def test_a_busy_lock_serves_the_file_its_holder_repaired(
    tmp_path, recorders, monkeypatch
):
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    _rewrite(manager, _break_memory("pet"))
    monkeypatch.setattr(user_profile_module, "_REPAIR_LOCK_TIMEOUT_SECONDS", 0.5)
    lock = manager._get_lock(USER)
    held = threading.Event()
    release = threading.Event()

    repair_now = threading.Event()

    def _holder() -> None:
        with lock:
            held.set()
            repair_now.wait(10)
            manager.get_profile(USER)  # repairs under the held lock
            release.wait(10)  # keep holding past the reader's timeout

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(5)
    # The holder repairs only after the reader has started waiting, and keeps
    # the lock until the reader has timed out and answered.
    timer = threading.Timer(0.2, repair_now.set)
    timer.start()
    try:
        profile, authoritative = manager.load_profile(USER)
    finally:
        release.set()
        holder.join(5)
        timer.cancel()

    assert _memory_keys(profile) == {"favorite_color", "coffee"}
    assert authoritative is True


def test_a_busy_lock_timeout_never_waits_on_a_pending_migration(
    tmp_path, recorders, monkeypatch
):
    """After the bounded wait, a re-read that is healthy but still owes a
    one-shot migration must not then block on the busy lock to persist it."""
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    healthy_unmigrated = json.loads(_path(manager).read_text(encoding="utf-8"))
    healthy_unmigrated["global_skill_defaults_migrated"] = False
    _path(manager).write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(user_profile_module, "_REPAIR_LOCK_TIMEOUT_SECONDS", 1.0)
    lock = manager._get_lock(USER)
    held = threading.Event()
    fix_now = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with lock:
            held.set()
            fix_now.wait(10)
            # The holder fixes the file (raw, unmigrated) and keeps the lock.
            _path(manager).write_text(json.dumps(healthy_unmigrated), encoding="utf-8")
            release.wait(10)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    assert held.wait(5)
    result: list = []
    reader = threading.Thread(
        target=lambda: result.append(manager.load_profile(USER)), daemon=True
    )
    # The reader's first read sees the broken file; the fix lands while it
    # waits, so its post-timeout re-read is healthy but unmigrated.
    timer = threading.Timer(0.2, fix_now.set)
    try:
        reader.start()
        timer.start()
        reader.join(4)
        assert not reader.is_alive(), "the timed-out reader blocked on a migration"
    finally:
        release.set()
        holder.join(5)
        timer.cancel()

    profile, authoritative = result[0]
    assert authoritative is True
    assert _memory_keys(profile) == {"favorite_color", "pet", "coffee"}


# --- unchanged: a missing profile is created and seeded ---------------------


def test_a_missing_profile_is_created_and_seeded(tmp_path, recorders):
    manager = UserProfileManager(tmp_path)

    profile = manager.get_profile("newcomer")

    assert profile.tool_preferences.default_thread_tools is not None
    assert _path(manager, "newcomer").exists()
    assert not _quarantined(manager, "newcomer")
    assert recorders.audits == []


def test_setup_hydrate_reads_a_utf16_profile_with_null_tool_preferences(tmp_path):
    """The wizard's reconfigure path reads the bootstrap profile raw: it must
    accept what the store accepts (UTF-16 here) and survive a ``null``
    ``tool_preferences`` instead of raising."""
    from nymeria.setup import finalize, family_catalog
    from nymeria.setup.hydrate import _hydrate_profile_picks
    from nymeria.setup.state import WizardState

    state = WizardState(root=tmp_path / "runtime")
    root = finalize.resolve_runtime_root(state, for_docker=False)
    data_dir = finalize.resolve_data_dir(state, root=root)
    kit = family_catalog.skill_kit_choices()[0].value
    profile_path = data_dir / "users" / "default" / "profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_bytes(
        json.dumps(
            {"user_id": "default", "tool_preferences": None, "enabled_global_skills": [kit]}
        ).encode("utf-16")
    )

    _hydrate_profile_picks(state, for_docker=False)

    assert state.extras["skill_kits"] == [kit]


# --- #406: the in-app row (the message cut at the cap) keeps the fix --------


def test_the_unreadable_alert_row_keeps_the_remedy_before_a_long_os_error(
    tmp_path, recorders, monkeypatch
):
    """#406 behavior 25: the cause (an OS error carrying a full path) goes
    last, so the row still says what to check."""
    from nymeria.core.notifications import NOTIFICATION_SUMMARY_MAX_CHARS

    monkeypatch.setattr(user_profile_module, "_unavailable_reported", set())
    manager = UserProfileManager(tmp_path)
    _seed(manager)
    path = _path(manager)
    real_read_bytes = Path.read_bytes
    strerror = "Input/output error" + " on a degraded network volume" * 5

    def _failing(self: Path) -> bytes:
        if self == path:
            raise OSError(5, strerror, str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _failing)

    manager.get_profile(USER)

    assert recorders.alerted.wait(5)
    message = recorders.alerts[0]
    row = message[:NOTIFICATION_SUMMARY_MAX_CHARS]
    assert row.startswith("[PROFILE UNREADABLE]")
    assert "Check its permissions and free disk space." in row
    # The cause still reaches external destinations whole.
    assert strerror in message and str(path) in message


def test_the_corrupt_alert_row_keeps_the_restore_hint(tmp_path, recorders):
    """#406 (CORRUPT twin of the TODO list copy): the restore hint leads the
    quarantine path, which grows with the account id."""
    from nymeria.core.notifications import NOTIFICATION_SUMMARY_MAX_CHARS

    user = "family-tablet-shared-account-" + "x" * 40
    manager = UserProfileManager(tmp_path)
    _seed(manager, user)
    _path(manager, user).write_bytes(b"{ not json")

    manager.get_profile(user)

    assert recorders.alerted.wait(5)
    message = recorders.alerts[0]
    row = message[:NOTIFICATION_SUMMARY_MAX_CHARS]
    assert row.startswith("[PROFILE CORRUPT]")
    assert "An admin can restore your memories from the original" in row
    quarantined = _quarantined(manager, user)
    assert len(quarantined) == 1
    assert f"users/{user}/quarantine/{quarantined[0].name}" in message
