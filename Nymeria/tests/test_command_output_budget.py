"""Per-surface command-output budgets.

The command layer used to cut EVERY result at 4,000 chars regardless of
who asked, with one hardcoded exemption for ``skills.show``. That is a
chat-platform-shaped limit, and it leaked onto the surfaces with real
scrollback: a `/provider list` in the terminal came back clipped
mid-row, which reads as a complete answer and is not one.

These tests pin the replacement: a budget keyed by surface, applied both
at the dispatcher and inside the handlers that pre-truncate their own
listings, plus a ratchet that fails when a built-in listing grows past
the compact budget (the remedy then is to split or filter that command,
never to let it truncate).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from nymeria.core import command_service as cs
from nymeria.core.command_service import OUTPUT_BUDGET_COMPACT, CommandSurface
from test_command_service import FakeCommandApi, _run_command

_TRUNCATION_MARKER = "Output truncated"

# Surfaces that render into a scrollback or a scrollable view.
ROOMY = ("cli", "desktop", "mobile", "api")
# Everything else, including the unknown-surface caller.
COMPACT = ("discord", "telegram", "slack", "whatsapp", "teams", "twitch", "agent")


class _BigEnvApi(FakeCommandApi):
    """An /env show payload far past any budget.

    ``_cmd_env_show`` renders every entry it is given with no per-entry
    cap of its own, so it is the honest way to exercise a HANDLER-side
    truncation rather than the dispatcher's.
    """

    def __init__(self, entries: int = 400) -> None:
        super().__init__()
        self.entry_count = entries

    async def get_env_vars(self, *, user_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_env_vars", (), {"user_id": user_id}))
        return {
            "entries": [
                {
                    "name": f"SETTING_NUMBER_{index:04d}",
                    "is_set": True,
                    "is_secret": False,
                    "category": "Bulk",
                    "value": "v" * 60,
                }
                for index in range(self.entry_count)
            ]
        }


def _long_handler(monkeypatch: pytest.MonkeyPatch, chars: int) -> None:
    """Make /memory list return *chars* of output, dispatcher-side only."""

    async def _fake(self: Any, bound: Any) -> str:
        return "x" * chars

    monkeypatch.setattr(cs._CommandExecutor, "_cmd_memory_list", _fake)


@pytest.mark.parametrize("surface", ROOMY)
def test_roomy_surfaces_keep_output_the_old_cap_would_have_cut(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    _long_handler(monkeypatch, 40_000)

    result = _run_command(FakeCommandApi(), "/memory list", surface=surface)

    assert result.success is True
    assert _TRUNCATION_MARKER not in result.markdown
    assert len(result.markdown) >= 40_000


@pytest.mark.parametrize("surface", COMPACT)
def test_compact_surfaces_truncate_at_the_shared_budget(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    _long_handler(monkeypatch, 40_000)

    result = _run_command(FakeCommandApi(), "/memory list", surface=surface)

    assert _TRUNCATION_MARKER in result.markdown
    assert len(result.markdown) <= OUTPUT_BUDGET_COMPACT
    # The note has to name the real size, or the reader cannot tell how
    # much they are missing.
    assert "40000" in result.markdown


def test_unknown_surface_gets_the_conservative_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that sends no surface is treated as compact.

    The roomy budget assumes a client that can scroll; assuming that of
    an unidentified caller is the wrong direction to guess in.
    """
    _long_handler(monkeypatch, 40_000)

    result = _run_command(FakeCommandApi(), "/memory list", surface=None)

    assert _TRUNCATION_MARKER in result.markdown
    assert len(result.markdown) <= OUTPUT_BUDGET_COMPACT


def test_a_real_long_listing_survives_on_a_roomy_surface() -> None:
    """End to end through a real handler, not a monkeypatched one.

    ``/env show`` used to cut its own listing at a private 4,000-char
    default before returning, so the text was already gone by the time
    the dispatcher measured it. Those handler-side cuts are deleted now
    (the dispatcher's per-surface budget is the only one), and this is
    the test that would catch either one coming back.
    """
    roomy = _run_command(_BigEnvApi(), "/env show", surface="cli")
    compact = _run_command(_BigEnvApi(), "/env show", surface="discord")

    assert _TRUNCATION_MARKER not in roomy.markdown
    assert len(roomy.markdown) > OUTPUT_BUDGET_COMPACT
    assert "SETTING_NUMBER_0399" in roomy.markdown

    assert _TRUNCATION_MARKER in compact.markdown
    assert len(compact.markdown) <= OUTPUT_BUDGET_COMPACT
    assert "SETTING_NUMBER_0399" not in compact.markdown


@pytest.mark.parametrize("surface", list(CommandSurface.__args__) + [None])
def test_no_surface_budget_falls_below_the_old_skills_show_exemption(
    monkeypatch: pytest.MonkeyPatch, surface: str | None
) -> None:
    """12k is a FLOOR now, not a one-command exemption.

    ``skills.show`` used to carry its own 12,000-char budget because a
    skill body plus its metadata table does not fit in 4,000. Retiring
    that special case is only safe while no surface budgets below it, so
    this asserts the delivered OUTPUT on every declared surface, not the
    relationship between two constants.
    """
    _long_handler(monkeypatch, 12_000)

    result = _run_command(FakeCommandApi(), "/memory list", surface=surface)

    assert _TRUNCATION_MARKER not in result.markdown
    assert len(result.markdown) >= 12_000


def test_truncation_never_returns_more_than_the_budget_it_was_given() -> None:
    """The note has to fit INSIDE the limit, not be added past it.

    The pre-existing implementation reserved room with a fixed `limit -
    80`, which goes negative once a caller passes a small limit; the
    function takes its limit from a caller now, so the boundary is
    reachable in principle rather than fixed at a constant.
    """
    from nymeria.core.command_service import _truncate

    text = "x" * (OUTPUT_BUDGET_COMPACT + 5_000)
    for limit in (200, 1_000, OUTPUT_BUDGET_COMPACT):
        out = _truncate(text, limit=limit)
        assert len(out) <= limit, (limit, len(out))
        assert _TRUNCATION_MARKER in out

    # Exactly at the budget is not truncation; one char over is.
    assert _TRUNCATION_MARKER not in _truncate("x" * 500, limit=500)
    assert _TRUNCATION_MARKER in _truncate("x" * 501, limit=500)

    # A limit too small to hold the note degrades to the note alone,
    # rather than returning MORE than it was asked for: `limit - 80`
    # went negative and sliced from the END, yielding the note plus a
    # tail of the text it was supposed to be cutting.
    tiny = _truncate(text, limit=10)
    assert tiny.strip().startswith("**Note:**")
    assert len(tiny) < 200


def test_built_in_listings_stay_well_inside_the_compact_budget() -> None:
    """Ratchet: a built-in must not grow into needing truncation.

    Truncating a listing is a last resort, not a design: a clipped table
    reads as a complete one. When this fails, SPLIT or filter the
    command (give it a shorter default view and an explicit opt-in to
    the rest, as `/provider list` does), do not raise the budget.

    Default views get a strict half-budget bar so the failure lands well
    before anything is actually cut. Explicit volume opt-ins (`--all`,
    `--tier unverified`) only have to stay under the real budget: asking
    for the firehose is asking for every row.

    SCOPE, honestly: this covers the CATALOG-driven listings, whose size
    tracks something in the repo and therefore grows without anyone
    noticing. It does NOT cover listings whose size tracks user data
    (`/memory list`, `/todos list`) or a live agent (`/tools list`,
    `/skills`), which are vacuous under this fixture -- `/tools list`
    renders 92 chars here because the fake thread has no tools, so
    including it would buy false confidence rather than coverage. Those
    carry their own per-listing caps instead (`/memory list` stops at 25
    rows with 200-char previews).
    """
    default_views = ("/provider list", "/provider list --tier cliproxy", "/help")
    for command in default_views:
        rendered = _run_command(FakeCommandApi(), command)
        assert rendered.success is True
        assert len(rendered.markdown) <= OUTPUT_BUDGET_COMPACT // 2, (
            f"{command} renders {len(rendered.markdown)} chars; give it a "
            "shorter default view rather than raising the budget"
        )

    opt_in_views = ("/provider list --all", "/provider list --tier unverified")
    for command in opt_in_views:
        rendered = _run_command(FakeCommandApi(), command)
        assert rendered.success is True
        assert _TRUNCATION_MARKER not in rendered.markdown
        assert len(rendered.markdown) <= OUTPUT_BUDGET_COMPACT, (
            f"{command} renders {len(rendered.markdown)} chars and now "
            "truncates on chat platforms; paginate it"
        )
