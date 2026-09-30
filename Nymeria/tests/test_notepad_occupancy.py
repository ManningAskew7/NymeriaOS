"""#101 entry 24: the thread notepad's cap is plannable, and hitting it names
the remedy a thread has (raising its own limit), not only "consolidate"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nymeria.tools import memory as memory_tools
from nymeria.tools import thread_notes

REMEDY = "`/memory limit <chars> thread`"
NUDGE = f"; nearly full: consolidate older notes, or raise this thread's limit with {REMEDY}"


@pytest.fixture
def notepad(monkeypatch, tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, memory_char_limit=100)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(memory_tools, "_profile_manager", None)
    monkeypatch.setattr(thread_notes, "_notes_dir", None)
    return settings


def _config() -> dict:
    return {"configurable": {"user_id": "user", "thread_id": "thread"}}


def _add(content: str) -> str:
    return memory_tools.memory_add.func(scope="thread", content=content, config=_config())


def _edit(find: str, replace: str) -> str:
    return memory_tools.memory_edit.func(scope="thread", find=find, replace=replace, config=_config())


def test_a_write_reports_how_full_the_notepad_is(notepad):
    assert _add("a" * 10) == (
        "[Saved]: Notepad updated (10 / 100 chars). This content will persist through compaction."
    )


@pytest.mark.parametrize("size, nudged", [(79, False), (80, True), (100, True)])
def test_from_80_percent_a_write_names_both_ways_forward(notepad, size, nudged):
    result = _add("a" * size)
    assert f"({size} / 100 chars{NUDGE if nudged else ''})" in result


def test_edits_report_occupancy_too(notepad):
    _add("alpha beta")
    assert _edit("beta", "gamma") == "[Saved]: Text replaced. Notepad is now 12 / 100 chars."
    assert _edit("", "x" * 90) == f"[Saved]: Notepad rewritten (91 / 100 chars{NUDGE})."


def test_the_cap_error_names_the_thread_limit_as_the_other_remedy(notepad):
    _add("a" * 60)
    result = _add("b" * 60)
    assert result.startswith("[Error]: Thread memory is full: character limit 100 exceeded")
    assert result.endswith(f"raise this thread's limit instead: {REMEDY}.")
    # Surface-neutral: the same string reaches a human's /notepad write and the
    # desktop Memory tab, so it names the command, not who should run it.
    assert "slash_command" not in result and "ask the user" not in result
    assert thread_notes.read_notepad("thread") == "a" * 60  # nothing saved

    for grown in (_edit("a" * 60, "c" * 120), _edit("", "z" * 150)):  # surgical, whole
        assert grown.startswith("[Error]: Thread memory is full") and REMEDY in grown
    assert thread_notes.read_notepad("thread") == "a" * 60


def test_a_notepad_left_over_a_lowered_cap_says_so_not_nearly_full(notepad):
    _add("a" * 90)
    notepad.memory_char_limit = 50  # the cap is lowered under the notes
    assert _edit("", "a" * 60) == (
        # 61: a rewrite stores a trailing newline, and the count is the stored size.
        "[Saved]: Notepad rewritten (61 / 50 chars; over the cap, so only edits that "
        f"shrink it succeed: consolidate older notes, or raise this thread's limit with {REMEDY})."
    )
    assert _add("b").startswith("[Error]: Thread memory is full")  # any growth fails


def test_a_dream_writing_its_parents_notepad_is_not_offered_a_raise_it_cannot_make(
    notepad, monkeypatch
):
    """A dream shadow's memory tools act on the PARENT's notepad, but a dream
    binds no slash_command (and one would act on the shadow), so its results
    name consolidation only; the parent's own writes keep the remedy."""
    configs = {"shadow": SimpleNamespace(shadow_parent_id="thread", memory_char_limit=None)}
    agent = SimpleNamespace(thread_config_manager=SimpleNamespace(get_config=configs.get))
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: agent)
    shadow = {"configurable": {"user_id": "user", "thread_id": "shadow"}}

    def dream_add(content: str) -> str:
        return memory_tools.memory_add.func(scope="thread", content=content, config=shadow)

    assert dream_add("a" * 85).endswith("(85 / 100 chars; nearly full: consolidate older notes). "
                                        "This content will persist through compaction.")
    assert thread_notes.read_notepad("thread") == "a" * 85  # landed on the parent
    refused = dream_add("b" * 20)
    assert refused.startswith("[Error]: Thread memory is full") and "/memory limit" not in refused
    rewritten = memory_tools.memory_edit.func(scope="thread", find="", replace="c" * 90, config=shadow)
    assert rewritten == "[Saved]: Notepad rewritten (91 / 100 chars; nearly full: consolidate older notes)."
    assert REMEDY in _edit("c" * 90, "d" * 90)  # the parent itself is still offered it


def test_global_memory_errors_do_not_offer_the_thread_remedy(notepad):
    notepad.memory_char_limit = 20
    config = _config()
    memory_tools.memory_add.func(scope="global", key="a", content="1234567890", config=config)
    result = memory_tools.memory_add.func(scope="global", key="b", content="1234567890", config=config)
    assert result.startswith("[Error]: Global memory is full")
    assert "/memory limit" not in result


def test_reading_the_notepad_adds_nothing_a_rewrite_could_copy_back(notepad):
    # The dream loop rewrites the notepad from what it read: no footer.
    _add("a" * 90)
    assert memory_tools.memory_read.func(scope="thread", config=_config()) == "a" * 90


@pytest.mark.parametrize("tool", [memory_tools.memory_add, memory_tools.memory_edit])
def test_the_tool_descriptions_state_the_cap_and_the_remedy(tool):
    # A tool binds without any kit text: its own description must carry this.
    assert REMEDY in tool.description
    assert "N / L chars" in tool.description


def test_the_add_description_prices_the_remedy_and_names_the_real_default():
    from nymeria.core.memory_limits import DEFAULT_MEMORY_CHAR_LIMIT

    # Raising the cap is not free: the whole notepad re-seeds after every compaction.
    description = " ".join(memory_tools.memory_add.description.split())
    assert "Raise it modestly" in description and "reloaded after every compaction" in description
    # The schema text states the default as a literal: pin it to the constant.
    assert f"({DEFAULT_MEMORY_CHAR_LIMIT} unless configured)" in description
