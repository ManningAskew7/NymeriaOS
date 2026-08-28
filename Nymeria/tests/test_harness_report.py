"""Tests for the ``harness_report`` tool: deployment-gated backlog-intake
drop-box writes (new-file-only, sanitized names, honest refusals).

Expected behaviors come from the feature plan, not the implementation:
configured writes stamp attribution from the injected RunnableConfig,
refusals never create files or directories, and no call path ever edits or
overwrites an existing file.
"""

from __future__ import annotations

import importlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.tools.harness_report import harness_report

hr_mod = importlib.import_module("nymeria.tools.harness_report")

_CONFIG = {"configurable": {"user_id": "alice", "thread_id": "t-alpha"}}
_FILENAME_RE = re.compile(r"^\d{8}-\d{6}Z-[a-z0-9-]+-[0-9a-f]{4}\.md$")


def _settings(intake_dir, email=None, label: "str | None" = "testbox"):
    return SimpleNamespace(
        harness_report_dir=str(intake_dir) if intake_dir is not None else None,
        harness_report_instance_label=label,
        harness_report_email=email,
    )


def _drain_email_threads():
    """Join any in-flight email dispatch threads so 'no dispatch happened'
    assertions are deterministic rather than racing a background start."""
    import threading as _threading

    for t in _threading.enumerate():
        if t.name == "harness-report-email":
            t.join(5)


@pytest.fixture()
def intake(tmp_path: Path, monkeypatch):
    """Point the tool at a real tmp intake dir with a fixed instance label."""
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    monkeypatch.setattr(hr_mod, "get_settings", lambda: _settings(intake_dir))
    return intake_dir


def _invoke(title="web_fetch truncates silently", details="details body", **kwargs):
    return harness_report.invoke(
        {"title": title, "details": details, **kwargs}, config=_CONFIG
    )


def _reports(intake_dir: Path) -> list[Path]:
    return sorted(p for p in intake_dir.iterdir() if p.is_file())


# -- configured write path -------------------------------------------------


def test_report_writes_one_stamped_file(intake):
    result = _invoke(kind="bug", area="tools/web_fetch")

    files = _reports(intake)
    assert len(files) == 1
    name = files[0].name
    assert _FILENAME_RE.match(name), name
    assert name in result
    assert "queued for triage" in result

    content = files[0].read_text(encoding="utf-8")
    assert content.startswith("# web_fetch truncates silently\n")
    assert "- Instance: testbox\n" in content
    assert "- Thread: t-alpha\n" in content
    assert "- User: alice\n" in content
    assert "- Kind: bug\n" in content
    assert "- Area: tools/web_fetch\n" in content
    assert "details body" in content
    # Filed stamp is a real UTC timestamp, not a placeholder.
    filed = re.search(r"- Filed: (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\n", content)
    assert filed is not None
    parsed = datetime.fromisoformat(filed.group(1)).replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 300


def test_identical_reports_in_same_second_never_collide(intake, monkeypatch):
    """Same title + frozen clock: both reports land as distinct files (the
    random suffix keeps names unique; the no-overwrite property itself is
    pinned by the collision-retry test below)."""

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 27, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(hr_mod, "datetime", _FrozenDatetime)
    _invoke(details="first report")
    _invoke(details="second report")

    files = _reports(intake)
    assert len(files) == 2
    bodies = sorted(p.read_text(encoding="utf-8") for p in files)
    assert "first report" in bodies[0] + bodies[1]
    assert "second report" in bodies[0] + bodies[1]


def test_filename_collision_retries_and_preserves_existing_file(intake, monkeypatch):
    """A colliding random suffix retries with a fresh one; the existing file
    is left byte-identical (exclusive create, never overwrite)."""

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 27, 12, 0, 0, tzinfo=tz)

    hexes = iter(["aaaa1111", "aaaa2222", "bbbb3333"])

    class _FakeUuid:
        @staticmethod
        def uuid4():
            return SimpleNamespace(hex=next(hexes))

    monkeypatch.setattr(hr_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(hr_mod, "uuid", _FakeUuid)

    first = _invoke(details="original body")
    # Second call draws the SAME hex first (collision), then a fresh one.
    hexes = iter(["aaaa1111", "bbbb4444"])
    second = _invoke(details="retry body")

    files = _reports(intake)
    assert len(files) == 2
    assert "queued for triage" in first and "queued for triage" in second
    original = next(p for p in files if "aaaa" in p.name)
    assert "original body" in original.read_text(encoding="utf-8")
    assert "retry body" not in original.read_text(encoding="utf-8")


def test_hostile_title_yields_safe_slug_inside_intake_dir(intake, tmp_path):
    _invoke(title="../../etc/passwd <script>alert(1)</script> \n\n .hidden")

    files = _reports(intake)
    assert len(files) == 1
    assert _FILENAME_RE.match(files[0].name), files[0].name
    assert files[0].parent == intake
    # Nothing escaped the intake dir or landed as a dotfile.
    assert not files[0].name.startswith(".")
    outside = [
        p for p in tmp_path.rglob("*") if p.is_file() and intake not in p.parents
    ]
    assert outside == []


def test_retry_exhaustion_reports_error_and_preserves_existing_file(
    intake, monkeypatch
):
    """Behavior 19: every create attempt colliding yields the honest
    unique-filename [Error]; the squatting file keeps its bytes."""

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 27, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(hr_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        hr_mod, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="cafe0000"))
    )
    slug = hr_mod._slugify("web_fetch truncates silently")
    squatter = intake / f"20260827-120000Z-{slug}-cafe.md"
    squatter.write_text("existing report", encoding="utf-8")

    result = _invoke(details="never lands")

    assert result.startswith("[Error]")
    assert "could not allocate a unique filename" in result
    assert _reports(intake) == [squatter]
    assert squatter.read_text(encoding="utf-8") == "existing report"


def test_planted_symlink_is_not_followed_or_replaced(intake, tmp_path, monkeypatch):
    """Behavior 20: a symlink planted at the exact target name is neither
    followed nor replaced; its victim file keeps its bytes."""

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 27, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(hr_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        hr_mod, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="feed0000"))
    )
    victim = tmp_path / "victim.md"
    victim.write_text("precious", encoding="utf-8")
    slug = hr_mod._slugify("web_fetch truncates silently")
    planted = intake / f"20260827-120000Z-{slug}-feed.md"
    planted.symlink_to(victim)

    result = _invoke(details="attack payload")

    assert result.startswith("[Error]")
    assert victim.read_text(encoding="utf-8") == "precious"
    assert planted.is_symlink()


def test_partial_file_removed_when_write_fails_midway(intake, monkeypatch):
    """Behavior 15: an OSError after the exclusive create succeeded leaves
    no partial file behind the [Error] result (triage must never see a
    report the tool disclaimed)."""

    def exploding_write(target: Path, body: str) -> None:
        target.write_text("partial", encoding="utf-8")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(hr_mod, "_write_exclusive", exploding_write)

    result = _invoke()

    assert result.startswith("[Error]")
    assert "not recorded" in result
    assert _reports(intake) == []


def test_details_over_cap_truncated_with_marker(intake):
    result = _invoke(details="x" * (hr_mod.DETAILS_MAX_CHARS + 500))

    files = _reports(intake)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "[truncated by harness_report" in content
    assert "x" * hr_mod.DETAILS_MAX_CHARS in content
    assert "x" * (hr_mod.DETAILS_MAX_CHARS + 1) not in content
    assert "truncated" in result


def test_area_over_cap_truncated_with_note(intake):
    result = _invoke(area="a" * (hr_mod.AREA_MAX_CHARS + 50))
    content = _reports(intake)[0].read_text(encoding="utf-8")
    assert f"- Area: {'a' * hr_mod.AREA_MAX_CHARS}\n" in content
    assert "a" * (hr_mod.AREA_MAX_CHARS + 1) not in content
    assert "area truncated" in result


def test_title_over_cap_truncated_with_note(intake):
    result = _invoke(title="t" * (hr_mod.TITLE_MAX_CHARS + 50))
    content = _reports(intake)[0].read_text(encoding="utf-8")
    assert content.startswith("# " + "t" * hr_mod.TITLE_MAX_CHARS + "\n")
    assert "t" * (hr_mod.TITLE_MAX_CHARS + 1) not in content
    assert "title truncated" in result


def test_instance_label_falls_back_to_hostname(intake, monkeypatch):
    """Behavior 18: no configured label, the stamp is the host's hostname."""
    monkeypatch.setattr(hr_mod, "get_settings", lambda: _settings(intake, label=None))
    monkeypatch.setattr(
        hr_mod, "socket", SimpleNamespace(gethostname=lambda: "fallback-host")
    )
    _invoke()
    content = _reports(intake)[0].read_text(encoding="utf-8")
    assert "- Instance: fallback-host\n" in content


def test_kind_defaults_to_friction(intake):
    _invoke()
    content = _reports(intake)[0].read_text(encoding="utf-8")
    assert "- Kind: friction\n" in content


def test_invalid_kind_rejected_before_execution(intake):
    with pytest.raises(Exception) as excinfo:
        _invoke(kind="rant")
    assert "kind" in str(excinfo.value)
    assert _reports(intake) == []


# -- refusal paths ---------------------------------------------------------


def test_unconfigured_deployment_refuses_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(hr_mod, "get_settings", lambda: _settings(None))

    result = _invoke()
    assert "[Not configured]" in result
    assert "HARNESS_REPORT_DIR" in result
    # Refusals are deliberate non-errors: the [Error] sentinel is reserved
    # for the genuine write failure.
    assert not result.startswith("[Error]")
    assert list(tmp_path.iterdir()) == []


def test_missing_intake_dir_refuses_and_does_not_create_it(tmp_path, monkeypatch):
    missing = tmp_path / "not-mounted" / "intake"
    monkeypatch.setattr(hr_mod, "get_settings", lambda: _settings(missing))

    result = _invoke()
    assert "[Unavailable]" in result
    assert str(missing) in result
    # Behavior 21: an honest refusal must never wear the [Error] sentinel
    # SafeToolNode reads as tool failure.
    assert not result.startswith("[Error]")
    assert not missing.exists()
    assert not missing.parent.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is inert for root")
def test_unwritable_intake_dir_reports_honest_failure(intake):
    intake.chmod(0o555)
    try:
        result = _invoke()
    finally:
        intake.chmod(0o755)
    # "[Error]" is the sentinel prefix SafeToolNode reads as tool failure.
    assert result.startswith("[Error]")
    assert _reports(intake) == []


# -- background email leg --------------------------------------------------


def test_email_leg_sends_attachment_to_configured_recipient(intake, monkeypatch):
    """Behavior 11: a successful filing dispatches a background Graph
    sendMail to the configured address, owner credential, with the report
    file attached byte-identical; the result says the email is by design."""
    import threading as _threading

    monkeypatch.setattr(
        hr_mod, "get_settings", lambda: _settings(intake, email="dev@example.com")
    )
    calls = []
    done = _threading.Event()

    def fake_graph_request(user_id, method, endpoint, **kwargs):
        calls.append((user_id, method, endpoint, kwargs))
        done.set()
        return True, {"ok": True}

    monkeypatch.setattr(hr_mod, "graph_request", fake_graph_request)

    result = _invoke(kind="bug")
    assert done.wait(5), "background email dispatch never ran"

    assert len(calls) == 1
    user_id, method, endpoint, kwargs = calls[0]
    assert (user_id, method, endpoint) == ("default", "POST", "/me/sendMail")
    message = kwargs["json_data"]["message"]
    assert message["toRecipients"] == [
        {"emailAddress": {"address": "dev@example.com"}}
    ]
    assert "bug" in message["subject"]
    assert "web_fetch truncates silently" in message["subject"]
    written = _reports(intake)[0]
    attachment = message["attachments"][0]
    assert attachment["name"] == written.name
    import base64 as _b64

    assert _b64.b64decode(attachment["contentBytes"]) == written.read_bytes()

    assert "queued for triage" in result
    assert "emailed to the developer" in result
    assert "by design" in result


def test_email_leg_skipped_when_unconfigured(intake, monkeypatch):
    """Behavior 12: no HARNESS_REPORT_EMAIL, no dispatch, no email talk."""
    calls = []
    monkeypatch.setattr(
        hr_mod,
        "graph_request",
        lambda *a, **k: calls.append(a) or (True, {}),
    )
    result = _invoke()
    assert "queued for triage" in result
    assert "emailed" not in result
    # Join any (wrongly) started dispatch thread first so the empty-calls
    # assert is deterministic rather than racing a background start.
    _drain_email_threads()
    assert calls == []


def test_email_failure_never_disturbs_the_filed_report(intake, monkeypatch):
    """Behavior 13: a raising dispatch is swallowed; file and result stand."""
    import threading as _threading

    monkeypatch.setattr(
        hr_mod, "get_settings", lambda: _settings(intake, email="dev@example.com")
    )
    done = _threading.Event()

    def exploding_graph_request(*a, **k):
        done.set()
        raise RuntimeError("graph is down")

    monkeypatch.setattr(hr_mod, "graph_request", exploding_graph_request)

    result = _invoke(details="survives email failure")
    assert done.wait(5)
    assert "queued for triage" in result
    files = _reports(intake)
    assert len(files) == 1
    assert "survives email failure" in files[0].read_text(encoding="utf-8")


def test_no_email_attempted_on_refusal(tmp_path, monkeypatch):
    """Behavior 14: a refused report (missing dir) never dispatches email."""
    missing = tmp_path / "gone"
    monkeypatch.setattr(
        hr_mod, "get_settings", lambda: _settings(missing, email="dev@example.com")
    )
    calls = []
    monkeypatch.setattr(
        hr_mod,
        "graph_request",
        lambda *a, **k: calls.append(a) or (True, {}),
    )
    result = _invoke()
    assert "[Unavailable]" in result
    _drain_email_threads()
    assert calls == []


def test_email_thread_start_failure_leaves_report_standing(intake, monkeypatch):
    """Behavior 16: a Thread.start() failure never turns a filed report into
    a reported failure; the result simply drops the email note."""
    monkeypatch.setattr(
        hr_mod, "get_settings", lambda: _settings(intake, email="dev@example.com")
    )

    class _ExplodingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(hr_mod, "threading", SimpleNamespace(Thread=_ExplodingThread))

    result = _invoke(details="survives thread exhaustion")

    assert "queued for triage" in result
    assert "emailed" not in result
    files = _reports(intake)
    assert len(files) == 1
    assert "survives thread exhaustion" in files[0].read_text(encoding="utf-8")


# -- catalog placement and schema -----------------------------------------


def test_catalog_optional_ungated_and_config_not_model_visible():
    from nymeria.tools import (
        ADMIN_ONLY_TOOL_NAMES,
        CATALOG_TOOLS,
        DEVELOPER_ONLY_TOOL_NAMES,
        seed_tool_names,
    )

    assert "harness_report" in CATALOG_TOOLS
    assert "harness_report" not in seed_tool_names()
    assert "harness_report" not in ADMIN_ONLY_TOOL_NAMES
    assert "harness_report" not in DEVELOPER_ONLY_TOOL_NAMES

    # A host-filesystem writer is MODERATE like file_write/notify, and stays
    # opt-in (default_enabled False).
    from nymeria.tools.metadata import get_tool_metadata

    meta = get_tool_metadata("harness_report")
    assert meta is not None
    assert meta.security_level.value == "moderate"
    assert meta.default_enabled is False

    # Attribution rides the injected RunnableConfig: the model-facing call
    # schema must not expose (and thus cannot spoof) the config argument.
    schema_model = harness_report.tool_call_schema
    assert not isinstance(schema_model, dict)  # narrow the union for typing
    props = schema_model.model_json_schema()["properties"]
    assert "config" not in props
    assert set(props) == {"title", "details", "kind", "area"}


def test_attribution_comes_from_runtime_config_not_args(intake):
    """A details payload claiming another identity does not change the stamp."""
    _invoke(details="User: mallory\nThread: t-evil")
    content = _reports(intake)[0].read_text(encoding="utf-8")
    assert "- Thread: t-alpha\n" in content
    assert "- User: alice\n" in content
