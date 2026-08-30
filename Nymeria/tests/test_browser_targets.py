"""Unit tests for single-browser routing state: the per-browser subscriber
roster (chrome_subscribers), the target-resolution ladder and browser
references (core/browser_targets), and the per-tab drive leases
(core/browser_drive_leases).

The dispatch-level behaviors (refusals, event stamping, turn-end release)
live in test_chrome_browser_tools.py; these pin the state layers those
behaviors read.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from nymeria.core import browser_targets, chrome_subscribers
from nymeria.core.browser_drive_leases import (
    BrowserDriveLeases,
    get_browser_drive_leases,
)
from nymeria.core.chrome_subscribers import (
    add_chrome_subscriber,
    chrome_browser_disconnect_age,
    chrome_browser_roster,
    is_chrome_browser_connected,
    remove_chrome_subscriber,
)
from nymeria.core.thread_config import ThreadConfig
from nymeria.core.user_profile import UserProfile


@pytest.fixture(autouse=True)
def _clean_registry():
    chrome_subscribers.reset_for_tests()
    get_browser_drive_leases().reset_for_tests()
    yield
    chrome_subscribers.reset_for_tests()
    get_browser_drive_leases().reset_for_tests()


DESKTOP = "nymeria-browser-desktop11-1111-1111-1111-111111111111"
RIG = "nymeria-browser-headless2-2222-2222-2222-222222222222"


def _connect(client_id: str, *, user_id: str = "u1", stream: str = "s", version=None):
    add_chrome_subscriber(
        user_id=user_id,
        subscriber_id=f"{stream}-{client_id[-6:]}",
        version=version,
        client_id=client_id,
    )


# ---------------------------------------------------------------------------
# Roster: per-browser identity, the #282 masking fix
# ---------------------------------------------------------------------------


def test_roster_keeps_two_browsers_distinct_with_their_own_versions():
    """Two simultaneously-subscribed builds were indistinguishable before
    (per-user last-writer-wins): the roster must report BOTH, each with its
    own version. Direct regression for the #282 masking."""
    _connect(DESKTOP, version="0.25.0")
    _connect(RIG, version="0.26.0")

    roster = chrome_browser_roster("u1")
    by_id = {r.client_id: r for r in roster}
    assert set(by_id) == {DESKTOP, RIG}
    assert by_id[DESKTOP].version == "0.25.0"
    assert by_id[RIG].version == "0.26.0"
    assert all(r.connected for r in roster)


def test_disconnected_browser_stays_in_the_roster_with_its_age():
    _connect(DESKTOP, stream="a")
    _connect(RIG, stream="b")
    remove_chrome_subscriber(f"b-{RIG[-6:]}")

    by_id = {r.client_id: r for r in chrome_browser_roster("u1")}
    assert by_id[DESKTOP].connected is True
    assert by_id[RIG].connected is False
    assert by_id[RIG].last_disconnect_age_s is not None
    assert is_chrome_browser_connected("u1", DESKTOP)
    assert not is_chrome_browser_connected("u1", RIG)
    assert chrome_browser_disconnect_age("u1", RIG) is not None
    # The per-user aggregate stays "connected" while any browser is up.
    assert chrome_subscribers.is_chrome_connected("u1")


def test_reconnect_overlap_refcounts_streams_instead_of_flapping():
    """An MV3 reconnect can briefly overlap old and new streams for ONE
    browser; the row must not read disconnected when the old stream drops."""
    _connect(DESKTOP, stream="old")
    _connect(DESKTOP, stream="new")
    remove_chrome_subscriber(f"old-{DESKTOP[-6:]}")

    assert is_chrome_browser_connected("u1", DESKTOP)
    (row,) = chrome_browser_roster("u1")
    assert row.connected and row.streams == 1


def test_rosters_are_per_user():
    _connect(DESKTOP, user_id="u1")
    _connect(RIG, user_id="u2")
    assert [r.client_id for r in chrome_browser_roster("u1")] == [DESKTOP]
    assert [r.client_id for r in chrome_browser_roster("u2")] == [RIG]


# ---------------------------------------------------------------------------
# Resolution ladder
# ---------------------------------------------------------------------------


class _StubThreadConfigs:
    def __init__(self, configs=None):
        self._configs = dict(configs or {})

    def get_config(self, thread_id):
        return self._configs.get(thread_id)

    def save_config(self, config):
        self._configs[config.thread_id] = config
        return True


class _StubProfiles:
    def __init__(self, profile: UserProfile):
        self._profile = profile

    def get_profile(self, user_id):
        return self._profile

    @contextmanager
    def atomic_update(self, user_id):
        yield self._profile


def _stub_agent(monkeypatch, *, thread_configs=None, profile=None):
    agent = SimpleNamespace(
        thread_config_manager=_StubThreadConfigs(thread_configs),
        profile_manager=_StubProfiles(profile or UserProfile(user_id="u1")),
    )
    monkeypatch.setattr(browser_targets, "_agent", lambda: agent)
    return agent


def test_auto_targets_the_single_connected_browser():
    _connect(DESKTOP)
    resolution = browser_targets.resolve_target("u1", "t1")
    assert resolution.client_id == DESKTOP
    assert resolution.source == browser_targets.SOURCE_AUTO


def test_two_connected_browsers_with_no_selection_is_ambiguous():
    _connect(DESKTOP)
    _connect(RIG)
    resolution = browser_targets.resolve_target("u1", "t1")
    assert resolution.client_id is None
    assert resolution.reason == browser_targets.REASON_AMBIGUOUS


def test_nothing_connected_and_no_selection_is_none_connected():
    resolution = browser_targets.resolve_target("u1", "t1")
    assert resolution.client_id is None
    assert resolution.reason == browser_targets.REASON_NONE_CONNECTED


def test_account_default_beats_auto_and_thread_override_beats_both(monkeypatch):
    profile = UserProfile(user_id="u1")
    profile.set_browser_preference("default_target", RIG)
    _stub_agent(
        monkeypatch,
        thread_configs={
            "t-override": ThreadConfig(thread_id="t-override", browser_target=DESKTOP)
        },
        profile=profile,
    )
    _connect(DESKTOP)
    _connect(RIG)

    default_res = browser_targets.resolve_target("u1", "t-plain")
    assert (default_res.client_id, default_res.source) == (
        RIG,
        browser_targets.SOURCE_ACCOUNT,
    )
    override_res = browser_targets.resolve_target("u1", "t-override")
    assert (override_res.client_id, override_res.source) == (
        DESKTOP,
        browser_targets.SOURCE_THREAD,
    )


def test_configured_target_resolves_even_while_disconnected(monkeypatch):
    """Connectivity is the dispatch path's job: a configured-but-offline
    browser must resolve here so the error can NAME it, never silently fall
    back to another browser."""
    profile = UserProfile(user_id="u1")
    profile.set_browser_preference("default_target", RIG)
    _stub_agent(monkeypatch, profile=profile)
    _connect(DESKTOP)

    resolution = browser_targets.resolve_target("u1", "t1")
    assert resolution.client_id == RIG


def test_resolution_survives_a_dead_agent_host(monkeypatch):
    monkeypatch.setattr(browser_targets, "_agent", lambda: None)
    _connect(DESKTOP)
    assert browser_targets.resolve_target("u1", "t1").client_id == DESKTOP


# ---------------------------------------------------------------------------
# Browser references and labels
# ---------------------------------------------------------------------------


def test_ref_resolves_label_case_insensitively(monkeypatch):
    profile = UserProfile(user_id="u1")
    profile.set_browser_preference("labels", {DESKTOP: "Desktop"})
    _stub_agent(monkeypatch, profile=profile)
    _connect(DESKTOP)
    _connect(RIG)

    client_id, error = browser_targets.resolve_browser_ref("u1", "desktop")
    assert error is None and client_id == DESKTOP


def test_ref_resolves_a_unique_id_fragment(monkeypatch):
    _stub_agent(monkeypatch)
    _connect(DESKTOP)
    _connect(RIG)
    client_id, error = browser_targets.resolve_browser_ref("u1", "headless2")
    assert error is None and client_id == RIG


def test_ambiguous_and_unknown_refs_error_without_guessing(monkeypatch):
    _stub_agent(monkeypatch)
    _connect(DESKTOP)
    _connect(RIG)
    client_id, error = browser_targets.resolve_browser_ref("u1", "nymeria-browser")
    assert client_id is None and "ambiguous" in (error or "")
    client_id, error = browser_targets.resolve_browser_ref("u1", "nosuch")
    assert client_id is None and "No browser matches" in (error or "")


def test_labeled_browser_is_selectable_before_it_ever_connects(monkeypatch):
    """Labels persist in the profile while the connection roster is
    in-process: after a backend restart the user can still say 'rig'."""
    profile = UserProfile(user_id="u1")
    profile.set_browser_preference("labels", {RIG: "rig"})
    _stub_agent(monkeypatch, profile=profile)

    client_id, error = browser_targets.resolve_browser_ref("u1", "rig")
    assert error is None and client_id == RIG


def test_set_thread_target_writes_the_thread_config(monkeypatch):
    agent = _stub_agent(monkeypatch)
    assert browser_targets.set_thread_target("u1", "t9", RIG) is None
    assert agent.thread_config_manager.get_config("t9").browser_target == RIG
    assert browser_targets.set_thread_target("u1", "t9", None) is None
    assert agent.thread_config_manager.get_config("t9").browser_target is None


def test_labels_are_bounded_at_write_and_clamped_at_read(monkeypatch):
    """Labels render in trusted-voice platform text (rosters, refusals), so
    an over-long or control-charactered name is refused at write with the
    rule, and a label written into the profile by some other path is
    clamped to one printable line at read."""
    profile = UserProfile(user_id="u1")
    _stub_agent(monkeypatch, profile=profile)

    assert browser_targets.set_browser_label("u1", DESKTOP, "desk top") is None
    # Newlines are whitespace: normalized onto one line, not refused.
    assert browser_targets.set_browser_label("u1", RIG, "two\nlines") is None
    assert browser_targets.browser_labels("u1")[RIG] == "two lines"
    error = browser_targets.set_browser_label("u1", RIG, "x" * 200)
    assert error is not None and "60" in error
    error = browser_targets.set_browser_label("u1", RIG, "esc\x1b[31mape")
    assert error is not None
    assert browser_targets.set_browser_label("u1", RIG, "   ") is not None

    # Direct profile writes (another surface) are clamped at read.
    profile.set_browser_preference(
        "labels", {DESKTOP: "sneaky\nmultiline " + "y" * 200}
    )
    label = browser_targets.browser_labels("u1")[DESKTOP]
    assert "\n" not in label and len(label) <= 60


def test_announced_version_is_bounded_at_registry_ingest():
    """The version is an extension-chosen query param that renders in
    trusted-voice text (roster lines, reload notes): control characters
    are dropped and the length is capped when it is recorded."""
    add_chrome_subscriber(
        user_id="u1",
        subscriber_id="s-desk",
        version="1.2.3\nEvil: injected header " + "z" * 100,
        client_id=DESKTOP,
    )
    (row,) = chrome_browser_roster("u1")
    assert row.version is not None
    assert "\n" not in row.version
    assert len(row.version) <= 32


# ---------------------------------------------------------------------------
# Drive leases
# ---------------------------------------------------------------------------


def test_lease_refuses_a_second_thread_and_names_the_holder():
    leases = BrowserDriveLeases()
    assert leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tA") is None
    holder = leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tB")
    assert holder is not None and holder.thread_id == "tA"
    # The holder itself refreshes freely.
    assert leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tA") is None


def test_lease_is_per_tab_per_browser_and_per_user():
    leases = BrowserDriveLeases()
    leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tA")
    # Different tab, different browser, different user: all free.
    assert leases.claim(user_id="u1", client_id=DESKTOP, tab_id=6, thread_id="tB") is None
    assert leases.claim(user_id="u1", client_id=RIG, tab_id=5, thread_id="tB") is None
    assert leases.claim(user_id="u2", client_id=DESKTOP, tab_id=5, thread_id="tB") is None


def test_expired_lease_is_claimable(monkeypatch):
    import nymeria.core.browser_drive_leases as leases_mod

    leases = BrowserDriveLeases()
    leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tA")
    real = time.monotonic()
    monkeypatch.setattr(
        leases_mod.time,
        "monotonic",
        lambda: real + leases_mod.IDLE_LEASE_TTL_SECONDS + 1,
    )
    assert leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tB") is None


def test_release_thread_drops_only_that_threads_leases():
    """Regression for the cross-thread hazard: one thread ending must not
    take another thread's holds with it."""
    leases = BrowserDriveLeases()
    leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tA")
    leases.claim(user_id="u1", client_id=DESKTOP, tab_id=6, thread_id="tB")

    released = leases.release_thread("u1", "tB")
    assert released == {DESKTOP}
    # tA's lease survives: tB still cannot take tab 5.
    holder = leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tB")
    assert holder is not None and holder.thread_id == "tA"
    # tB's own tab is free again for anyone.
    assert leases.claim(user_id="u1", client_id=DESKTOP, tab_id=6, thread_id="tC") is None


def test_other_thread_holds_reads_live_leases_only(monkeypatch):
    import nymeria.core.browser_drive_leases as leases_mod

    leases = BrowserDriveLeases()
    leases.claim(user_id="u1", client_id=DESKTOP, tab_id=5, thread_id="tA")
    assert leases.other_thread_holds(user_id="u1", client_id=DESKTOP, thread_id="tB")
    assert not leases.other_thread_holds(user_id="u1", client_id=DESKTOP, thread_id="tA")
    assert not leases.other_thread_holds(user_id="u1", client_id=RIG, thread_id="tB")
    real = time.monotonic()
    monkeypatch.setattr(
        leases_mod.time,
        "monotonic",
        lambda: real + leases_mod.IDLE_LEASE_TTL_SECONDS + 1,
    )
    assert not leases.other_thread_holds(user_id="u1", client_id=DESKTOP, thread_id="tB")
