"""#102: the wizard's core-toolset step is an honest, interactive checklist of
the real seed, and what the operator keeps is what every new thread gets."""

from __future__ import annotations

import asyncio
import json

import pytest
from _setup_wizard_helpers import _capture_console, _first_run  # type: ignore[import-not-found]

from nymeria.setup.state import WizardState
from nymeria.setup.steps import core_tools as core_step
from nymeria.setup.tool_seed import (
    core_seed_tool_names,
    default_thread_tools_for_state,
    docker_init_seed_env,
)


def _drive(keys: list[str], state: WizardState | None = None) -> tuple[WizardState, dict]:
    """Run the step alone, press ``keys``, and return the state plus what the
    screen showed on mount (row labels, checked values, the first description)."""
    from textual.widgets import SelectionList, Static

    from nymeria.setup.app import SetupWizardApp

    state = state or WizardState()

    async def drive() -> dict:
        app = SetupWizardApp(state, steps=[core_step.make_core_tools_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            listing = app.screen.query_one(SelectionList)
            seen = {
                "rows": [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)],
                "checked": list(listing.selected),
                "desc": str(app.screen.query_one("#choice-desc", Static).render()),
            }
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
        return seen

    seen = asyncio.run(drive())
    return state, seen


# -- the screen ---------------------------------------------------------------


def test_every_seed_tool_is_a_checked_row_and_enter_keeps_them_all():
    state, seen = _drive(["enter"])
    core = core_seed_tool_names()
    assert seen["rows"] == core
    assert seen["checked"] == core
    assert seen["desc"]  # the first row explains itself
    assert state.extras["core_tools"] == core
    assert default_thread_tools_for_state(state) == default_thread_tools_for_state(WizardState())


def test_unticked_tools_leave_the_seed_and_the_rest_keep_seed_order():
    core = core_seed_tool_names()
    first, third = core[0], core[2]
    state, _ = _drive(["space", "down", "down", "space", "enter"])
    kept = [name for name in core if name not in {first, third}]
    assert state.extras["core_tools"] == kept
    tools = default_thread_tools_for_state(state)
    assert first not in tools and third not in tools
    assert tools[: len(kept)] == kept


def test_a_recorded_demote_opens_unticked():
    """A reconfigure must show what the operator kept, not the full seed:
    pressing Enter on a wrongly pre-checked screen would restore the demote."""
    core = core_seed_tool_names()
    state = WizardState()
    state.extras["core_tools"] = core[1:]
    state, seen = _drive(["enter"], state)
    assert seen["checked"] == core[1:]
    assert state.extras["core_tools"] == core[1:]


def test_the_highlighted_description_stays_on_screen_at_80x24():
    """19 rows under the note used to push the description below the fold on
    a small terminal; the list now scrolls inside its own box."""
    from textual.widgets import Static

    from nymeria.setup.app import SetupWizardApp

    async def drive() -> tuple:
        app = SetupWizardApp(WizardState(), steps=[core_step.make_core_tools_step()])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for _ in range(len(core_seed_tool_names()) - 1):  # the last, longest row
                await pilot.press("down")
            await pilot.pause()
            body = app.screen.query_one("#wizard-body").region
            desc = app.screen.query_one("#choice-desc", Static)
            return body, desc.region, str(desc.render())

    body, desc, text = asyncio.run(drive())
    assert text.startswith(core_step.CORE_TOOL_NOTES["wait_for_reply"])
    assert body.y <= desc.y and desc.bottom <= body.bottom


def test_skipping_the_step_records_no_decision():
    state, _ = _drive(["ctrl+s"])
    assert "core_tools" not in state.extras
    assert default_thread_tools_for_state(state)[: len(core_seed_tool_names())] == core_seed_tool_names()


def test_the_description_follows_the_cursor_to_slash_command():
    from textual.content import Content

    from nymeria.setup.steps.base import code_markup

    core = core_seed_tool_names()
    target = core.index("slash_command")
    assert target > 0  # the cursor has to MOVE for the panel check to mean anything
    from textual.widgets import Static

    from nymeria.setup.app import SetupWizardApp

    async def drive() -> str:
        app = SetupWizardApp(WizardState(), steps=[core_step.make_core_tools_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(target):
                await pilot.press("down")
            await pilot.pause()
            return str(app.screen.query_one("#choice-desc", Static).render())

    shown = asyncio.run(drive())
    expected = Content.from_markup(code_markup(core_step.core_tool_description("slash_command"))).plain
    assert shown == expected
    assert "self-management" in shown and "as if it were you" in shown
    assert "/kit" not in shown and "/skill" not in shown


def test_the_note_is_honest_about_the_seed():
    note = core_step.CORE_TOOLS_NOTE
    assert "editable default set, not a fixed one" in note
    assert "not a block; a thread can still enable it" in note
    assert "`/tools enable|disable <name> global`" in note and "Settings, Tools" in note
    assert "`Skill`, added automatically" in note


# -- row text -------------------------------------------------------------------


def test_every_seed_tool_has_a_user_facing_line():
    for choice in core_step.core_tool_choices():
        assert choice.description.strip(), choice.value
        assert choice.label == choice.value


@pytest.mark.parametrize("name", ["reply_to_thread", "wait_for_reply"])
def test_rows_whose_absence_breaks_a_contract_say_what_stops_working(name):
    from nymeria.tools.metadata import CAPABILITY_LOSS_NOTES

    text = core_step.core_tool_description(name)
    assert text == f"{core_step.CORE_TOOL_NOTES[name]} If unticked: {CAPABILITY_LOSS_NOTES[name]}."


def test_a_seed_tool_without_a_curated_line_shows_its_own_first_sentence(monkeypatch):
    monkeypatch.delitem(core_step.CORE_TOOL_NOTES, "notify")
    text = core_step.core_tool_description(
        "notify", "Send a notification to the user. Second sentence is agent detail."
    )
    assert text == "Send a notification to the user."


# -- what finalize writes -------------------------------------------------------


def test_a_kept_list_is_read_in_seed_order_and_a_name_that_left_the_seed_is_dropped():
    core = core_seed_tool_names()
    state = WizardState()
    state.extras["core_tools"] = ["retired_tool", core[3], core[1]]
    tools = default_thread_tools_for_state(state)
    assert tools[:2] == [core[1], core[3]]
    assert "retired_tool" not in tools


def test_unticking_every_core_tool_leaves_only_the_picks():
    # An empty kept list is a decision, not "no answer": nothing is restored.
    state = WizardState()
    state.extras["core_tools"] = []
    assert default_thread_tools_for_state(state) == ["web_search_ddgs", "fetch_url_nymeria"]


def test_the_review_screen_counts_the_unticked_core_tools():
    from nymeria.setup.steps.review import _summary_markup

    core = core_seed_tool_names()
    assert "(core set + your picks)" in _summary_markup(WizardState())
    state = WizardState()
    state.extras["core_tools"] = core[2:]
    markup = _summary_markup(state)
    assert f"Default thread tools: {len(core)} (core set minus 2 unticked + your picks)" in markup


def test_the_docker_carrier_carries_a_reduced_seed():
    from nymeria.config.init_seed_env import INIT_DEFAULT_THREAD_TOOLS_ENV

    core = core_seed_tool_names()
    state = WizardState()
    assert INIT_DEFAULT_THREAD_TOOLS_ENV not in docker_init_seed_env(state)
    state.extras["core_tools"] = core[1:]
    carried = docker_init_seed_env(state)[INIT_DEFAULT_THREAD_TOOLS_ENV]
    assert core[0] not in carried.split(":")
    assert carried.split(":")[: len(core) - 1] == core[1:]


def test_a_seed_tool_missing_from_disk_is_not_read_as_declined():
    """A profile or carrier written before a tool was promoted into the seed
    lacks it too, and the backend never backfills one on restart: without the
    wizard's own record, absence means nothing and the reconfigure restores
    the full seed, as it always did."""
    from nymeria.config.init_seed_env import INIT_DEFAULT_THREAD_TOOLS_ENV
    from nymeria.setup.hydrate import _apply_tool_picks, _hydrate_carrier_picks

    core = core_seed_tool_names()
    old_list = [t for t in core if t not in {"spawn_thread", "reply_to_thread"}]
    from_profile = WizardState()
    _apply_tool_picks(from_profile, [*old_list, "web_search_ddgs"])
    assert "core_tools" not in from_profile.extras
    assert default_thread_tools_for_state(from_profile)[: len(core)] == core

    from_carrier = WizardState()
    _hydrate_carrier_picks(from_carrier, {INIT_DEFAULT_THREAD_TOOLS_ENV: ":".join(old_list)})
    assert "core_tools" not in from_carrier.extras


def test_the_declined_record_is_the_only_source_of_a_demote():
    from nymeria.config.init_seed_env import INIT_DECLINED_CORE_TOOLS_ENV
    from nymeria.setup.hydrate import _hydrate_declined_core

    core = core_seed_tool_names()
    state = WizardState()
    _hydrate_declined_core(state, {INIT_DECLINED_CORE_TOOLS_ENV: "bash_execute:retired_tool"})
    # Every current seed tool but the declined one: one promoted since the
    # record was written is kept; a recorded name no longer seeded is moot.
    assert state.extras["core_tools"] == [t for t in core if t != "bash_execute"]

    nothing = WizardState()
    _hydrate_declined_core(nothing, {})
    assert "core_tools" not in nothing.extras

    decided = WizardState()
    decided.extras["core_tools"] = list(core)
    _hydrate_declined_core(decided, {INIT_DECLINED_CORE_TOOLS_ENV: "bash_execute"})
    assert decided.extras["core_tools"] == core  # this run's own pick wins


def _profile_tools(root) -> list[str]:
    raw = json.loads((root / "data" / "users" / "default" / "profile.json").read_text())
    return raw["tool_preferences"]["default_thread_tools"]


def _reconfigure(root, section: str, **extras) -> WizardState:
    from nymeria.setup.finalize import finalize
    from nymeria.setup.hydrate import hydrate_state_from_disk

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.extras.update(extras)
    console, _ = _capture_console()
    assert finalize(state, console=console, non_interactive=False,
                    overwrite_confirmed=True, merge=True, scoped_section=section) == 0
    return state


def test_a_demote_survives_later_reconfigures_and_re_ticking_retires_it(monkeypatch, tmp_path):
    from nymeria.config.init_seed_env import INIT_DECLINED_CORE_TOOLS_ENV

    core = core_seed_tool_names()
    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)
    config = root / "config.env"
    assert INIT_DECLINED_CORE_TOOLS_ENV not in config.read_text()

    _reconfigure(root, "core_tools", core_tools=[t for t in core if t != "bash_execute"])
    assert "bash_execute" not in _profile_tools(root)
    assert f"{INIT_DECLINED_CORE_TOOLS_ENV}=bash_execute" in config.read_text()

    # An unrelated section later: the record keeps the demote.
    _reconfigure(root, "web_search", web_search=["web_search_brave"])
    tools = _profile_tools(root)
    assert "bash_execute" not in tools and "web_search_brave" in tools

    _reconfigure(root, "core_tools", core_tools=list(core))
    assert set(core) <= set(_profile_tools(root))
    assert INIT_DECLINED_CORE_TOOLS_ENV not in config.read_text()


def test_a_declined_tool_turned_back_on_stays_on(monkeypatch, tmp_path):
    """`/tools enable bash_execute global` after unticking it at init: the
    profile is the newer word, so a later reconfigure keeps the tool and
    retires the stale record."""
    from nymeria.config.init_seed_env import INIT_DECLINED_CORE_TOOLS_ENV
    from nymeria.core.user_profile import UserProfileManager

    core = core_seed_tool_names()
    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)
    _reconfigure(root, "core_tools", core_tools=[t for t in core if t != "bash_execute"])
    manager = UserProfileManager(root / "data")
    profile = manager.get_profile("default")
    profile.tool_preferences.default_thread_tools = [*_profile_tools(root), "bash_execute"]
    manager.save_profile(profile)

    state = _reconfigure(root, "web_search", web_search=["web_search_ddgs"])
    assert "bash_execute" in _profile_tools(root)
    assert "core_tools" not in state.extras
    assert INIT_DECLINED_CORE_TOOLS_ENV not in (root / "config.env").read_text()


def test_a_tool_promoted_after_a_demote_arrives_on_the_next_reconfigure(monkeypatch, tmp_path):
    from nymeria.core.user_profile import UserProfileManager

    core = core_seed_tool_names()
    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)
    _reconfigure(root, "core_tools", core_tools=[t for t in core if t != "bash_execute"])
    # Simulate a list written before spawn_thread joined the seed.
    manager = UserProfileManager(root / "data")
    profile = manager.get_profile("default")
    profile.tool_preferences.default_thread_tools = [
        t for t in _profile_tools(root) if t != "spawn_thread"
    ]
    manager.save_profile(profile)

    state = _reconfigure(root, "web_search", web_search=["web_search_ddgs"])
    tools = _profile_tools(root)
    assert "spawn_thread" in tools and "bash_execute" not in tools
    assert state.extras["core_tools"] == [t for t in core if t != "bash_execute"]


def test_a_scoped_reconfigure_keeps_skills_the_wizard_does_not_manage(monkeypatch, tmp_path):
    """Pre-existing: a profile-pick update rewrote enabled_global_skills from
    the kit picks alone, dropping any other skill; `init core_tools` is now
    one more way to trigger it."""
    from nymeria.core.user_profile import UserProfileManager

    root = tmp_path / "runtime"
    _first_run(monkeypatch, root)
    manager = UserProfileManager(root / "data")
    profile = manager.get_profile("default")
    profile.enabled_global_skills = [*profile.enabled_global_skills, "my-own-skill"]
    manager.save_profile(profile)

    _reconfigure(root, "core_tools", core_tools=core_seed_tool_names()[1:])

    raw = json.loads((root / "data" / "users" / "default" / "profile.json").read_text())
    assert "my-own-skill" in raw["enabled_global_skills"]


def test_the_docker_env_file_carries_the_declined_record(tmp_path):
    from nymeria.config.init_seed_env import INIT_DECLINED_CORE_TOOLS_ENV
    from nymeria.setup.finalize import write_config

    env = tmp_path / ".env.docker"
    write_config(env, data_dir=tmp_path / "data", for_docker=True,
                 declined_core_tools=["bash_execute", "notify"])
    assert f"{INIT_DECLINED_CORE_TOOLS_ENV}=bash_execute:notify" in env.read_text()


def test_a_scoped_core_tools_reconfigure_rewrites_the_profile(tmp_path, monkeypatch):
    """`nymeria init core_tools` must reach the profile: the bootstrap
    profile updater used to ignore any section but the family pickers."""
    from nymeria.setup.finalize import seed_bootstrap_profile, update_bootstrap_profile

    core = core_seed_tool_names()
    data_dir = tmp_path / "data"
    console, _ = _capture_console()
    seed_bootstrap_profile(data_dir, WizardState(), console)
    profile = data_dir / "users" / "default" / "profile.json"
    before = json.loads(profile.read_text())["tool_preferences"]["default_thread_tools"]
    assert before[: len(core)] == core

    state = WizardState()
    state.extras["core_tools"] = [name for name in core if name != "bash_execute"]
    update_bootstrap_profile(data_dir, state, console, scoped_section="core_tools")

    after = json.loads(profile.read_text())["tool_preferences"]["default_thread_tools"]
    assert "bash_execute" not in after
    assert after[: len(core) - 1] == [name for name in core if name != "bash_execute"]
