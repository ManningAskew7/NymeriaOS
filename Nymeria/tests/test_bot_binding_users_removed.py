"""Guard: the dead ``_binding_users`` map must never return to the chat bots.

Slice 22 F3 (2026-06-27) deleted ``self._binding_users`` from all 13 chat-platform
bots in ``nymeria/triggers/``. It was write-only dead state: populated in
``_cmd_bind``, cleared in ``_cmd_unbind``, and rebuilt in
``_refresh_bindings``/``refresh_bindings``, but never read for routing,
authorization, or display. Cross-user thread isolation is enforced SERVER-SIDE by
``_require_thread_access`` (``triggers/api.py``), which 404s a non-admin act-as
caller against any personal thread they do not own; a channel binding confers no
authorization. A client-side bot dict is the wrong layer for an access control,
so this guard fails loudly if any bot module reintroduces the token. If per-user
gating within shared channels is ever wanted, it belongs in a server-side ACL
keyed on ``thread_id`` inside ``_require_thread_access``, not here.
"""

from __future__ import annotations

from pathlib import Path

TRIGGERS_ROOT = Path(__file__).resolve().parent.parent / "nymeria" / "triggers"

# The 13 bots that historically carried the dead map. The guard scans every
# ``*_bot.py`` (so a brand-new bot is covered too); this set anchors the floor
# test so the scan can never pass vacuously.
_HISTORICAL_BOTS = {
    "rocketchat_bot.py",
    "mattermost_bot.py",
    "zulip_bot.py",
    "teams_bot.py",
    "google_chat_bot.py",
    "line_bot.py",
    "matrix_bot.py",
    "slack_bot.py",
    "signal_bot.py",
    "instagram_bot.py",
    "messenger_bot.py",
    "webex_bot.py",
    "whatsapp_bot.py",
}


def _bot_source_files() -> list[Path]:
    return sorted(TRIGGERS_ROOT.glob("*_bot.py"))


def test_no_bot_module_reintroduces_binding_users() -> None:
    offenders = [
        path.name
        for path in _bot_source_files()
        if "_binding_users" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "_binding_users is write-only dead state removed in slice 22 F3; do not "
        f"reintroduce it (offending files: {offenders}). Cross-user thread "
        "isolation is enforced server-side by _require_thread_access, not by a "
        "bot-process map."
    )


def test_guard_actually_scans_the_historical_bots() -> None:
    # Floor test: the glob must find every bot that used to carry the map, so the
    # reintroduction guard above cannot pass against an empty file set.
    found = {path.name for path in _bot_source_files()}
    missing = _HISTORICAL_BOTS - found
    assert not missing, f"guard glob missed expected bot modules: {sorted(missing)}"
