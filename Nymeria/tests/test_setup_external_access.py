"""External-access automation: helpers, env writes, hydrate, and the SSE probe.

Covers nymeria/setup/external_access.py (tailscale parsing, the Cloudflare
API client against a mock transport, URL/SSE verification), the finalize env
writes (NYMERIA_EXTERNAL_ACCESS / NYMERIA_PUBLIC_URL / CORS_ORIGINS), the
hydrate round-trip, and the /health/stream endpoint the SSE check probes.
"""

import asyncio
import json
import re

import httpx
import pytest

from nymeria.setup import external_access as ea
from nymeria.setup.state import WizardState

from tests.test_setup_wizard import _stub_llm  # noqa: F401  (fixture-style helper)
from nymeria.setup.runner import main as setup_main


# --- tailscale parsing -------------------------------------------------------


def test_parse_tailscale_status_tolerates_noise_and_trailing_dot():
    raw = (
        "Warning: something something\n"
        '{"BackendState": "Running", "Self": {"DNSName": "nym.tail42.ts.net.",'
        ' "TailscaleIPs": ["100.64.1.2"]}}'
    )
    status = ea.parse_tailscale_status(raw)
    assert status is not None
    assert status.logged_in
    assert status.dns_name == "nym.tail42.ts.net"
    assert status.https_url == "https://nym.tail42.ts.net"


def test_tailscale_https_url_requires_magicdns_name():
    # No IP fallback: the serve cert is minted for the DNS name only, so an
    # https://100.x.y.z origin could never verify.
    no_dns = ea.TailscaleStatus(
        backend_state="Running", dns_name="", tailscale_ips=("100.64.1.2",)
    )
    assert no_dns.https_url is None
    bare = ea.TailscaleStatus(backend_state="NeedsLogin", dns_name="", tailscale_ips=())
    assert not bare.logged_in
    assert bare.https_url is None


def test_parse_tailscale_status_rejects_garbage_and_tolerates_trailing_noise():
    assert ea.parse_tailscale_status("no json here") is None
    assert ea.parse_tailscale_status("{not valid") is None
    # Noise AFTER the JSON document must not break parsing either.
    trailing = '{"BackendState": "Running", "Self": {}}\nWarning: tailing noise'
    status = ea.parse_tailscale_status(trailing)
    assert status is not None and status.logged_in


def test_extract_tailscale_auth_url_from_json_and_plain_output():
    json_line = json.dumps(
        {"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/a/abc"}
    )
    assert (
        ea.extract_tailscale_auth_url(json_line)
        == "https://login.tailscale.com/a/abc"
    )
    plain = "To authenticate, visit: https://login.tailscale.com/a/xyz"
    assert ea.extract_tailscale_auth_url(plain) == "https://login.tailscale.com/a/xyz"
    assert ea.extract_tailscale_auth_url('{"BackendState": "Starting"}') is None
    assert ea.extract_tailscale_auth_url("plain text") is None


def test_extract_tailscale_auth_url_from_real_multiline_marshalindent_output():
    # The real client emits the status block as MULTI-LINE indented JSON
    # (json.MarshalIndent), so the URL arrives on a quoted field line; the
    # extracted URL must not carry the closing quote/comma.
    block = (
        "{\n"
        '\t"BackendState": "NeedsLogin",\n'
        '\t"AuthURL": "https://login.tailscale.com/a/0123456789abcdef",\n'
        '\t"QR": "data:image/png;base64,xyz"\n'
        "}\n"
    )
    found = [
        url
        for line in block.splitlines()
        if (url := ea.extract_tailscale_auth_url(line)) is not None
    ]
    assert found == ["https://login.tailscale.com/a/0123456789abcdef"]


# --- origin / CORS derivation --------------------------------------------------


def test_public_origin_normalizes_to_browser_origin_form():
    assert ea.public_origin("https://nym.example.com/") == "https://nym.example.com"
    assert (
        ea.public_origin("https://nym.example.com:8443/some/path")
        == "https://nym.example.com:8443"
    )
    # Browser Origin headers are lowercase with no default port; CORS matches
    # exact strings, so the written origin must take the same form.
    assert (
        ea.public_origin("HTTPS://Nym.Example.com:443/") == "https://nym.example.com"
    )
    assert ea.public_origin("http://host.example.com:80") == "http://host.example.com"
    # Userinfo must never reach the env file, and trailing host dots never
    # match a browser Origin; both are rebuilt away from hostname/port.
    assert (
        ea.public_origin("https://user:Secret@nym.example.com/")
        == "https://nym.example.com"
    )
    assert ea.public_origin("https://nym.example.com.:443") == "https://nym.example.com"
    assert ea.public_origin("https://[2001:db8::1]:8443/x") == "https://[2001:db8::1]:8443"
    # Not a URL: returned trimmed rather than mangled.
    assert ea.public_origin("nym.example.com") == "nym.example.com"


def test_merged_cors_origins_seeds_default_appends_and_dedups():
    from nymeria.config.settings import DEFAULT_CORS_ORIGINS

    merged = ea.merged_cors_origins("", "https://nym.tail42.ts.net/")
    assert merged.startswith(DEFAULT_CORS_ORIGINS)
    assert merged.endswith(",https://nym.tail42.ts.net")

    existing = "https://app.example.com,https://nym.tail42.ts.net"
    assert ea.merged_cors_origins(existing, "https://nym.tail42.ts.net") == existing


# --- cloudflare client -----------------------------------------------------------


def _cf_transport(handler):
    return httpx.MockTransport(handler)


def _ok(result):
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


def test_tunnel_name_for_hostname_is_per_hostname():
    # Per-hostname names: a shared fixed name would make a second install a
    # load-balanced replica of the first install's tunnel.
    assert ea.tunnel_name_for_hostname("nymeria.example.com") == (
        "nymeria-nymeria-example-com"
    )
    assert ea.tunnel_name_for_hostname("A_B.example.com").startswith("nymeria-a-b")
    # Truncation of very long hostnames must not collide two of them.
    long_a = ea.tunnel_name_for_hostname("a" * 70 + ".example.com")
    long_b = ea.tunnel_name_for_hostname("a" * 70 + "b.example.com")
    assert len(long_a) <= 63 and len(long_b) <= 63
    assert long_a != long_b


def test_cloudflare_client_drives_the_named_tunnel_recipe():
    tunnel_name = ea.tunnel_name_for_hostname("nymeria.example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/client/v4")
        if path == "/user/tokens/verify":
            return _ok({"status": "active"})
        if path == "/zones":
            # Exact-name lookups: the full hostname is not a zone, its parent is.
            name = request.url.params.get("name")
            if name == "example.com":
                return _ok(
                    [{"id": "zone-1", "name": "example.com", "account": {"id": "acct-1"}}]
                )
            return _ok([])
        if path == "/accounts/acct-1/cfd_tunnel" and request.method == "GET":
            return _ok([])  # no existing tunnel: force the create path
        if path == "/accounts/acct-1/cfd_tunnel" and request.method == "POST":
            body = json.loads(request.content)
            assert body == {"name": tunnel_name, "config_src": "cloudflare"}
            return _ok({"id": "tun-1", "token": "tunnel-token"})
        if path == "/accounts/acct-1/cfd_tunnel/tun-1/configurations":
            body = json.loads(request.content)
            assert body["config"]["ingress"][0] == {
                "hostname": "nymeria.example.com",
                "service": "http://localhost:8000",
            }
            return _ok({})
        if path == "/zones/zone-1/dns_records" and request.method == "GET":
            return _ok([])
        if path == "/zones/zone-1/dns_records" and request.method == "POST":
            body = json.loads(request.content)
            assert body["content"] == "tun-1.cfargotunnel.com"
            assert body["proxied"] is True
            return _ok({})
        if path == "/accounts/acct-1/cfd_tunnel/tun-1":
            return _ok({"status": "healthy"})
        raise AssertionError(f"unexpected call {request.method} {path}")

    async def drive():
        client = ea.CloudflareTunnelClient("tok", transport=_cf_transport(handler))
        try:
            await client.verify_token()
            zone_id, zone_name, account_id = await client.find_zone(
                "nymeria.example.com"
            )
            tunnel_id, tunnel_token = await client.get_or_create_tunnel(
                account_id, tunnel_name
            )
            await client.put_ingress(account_id, tunnel_id, "nymeria.example.com")
            await client.ensure_dns_record(zone_id, "nymeria.example.com", tunnel_id)
            healthy = await client.tunnel_healthy(account_id, tunnel_id)
        finally:
            await client.aclose()
        return zone_name, account_id, tunnel_id, tunnel_token, healthy

    zone_name, account_id, tunnel_id, tunnel_token, healthy = asyncio.run(drive())
    assert (zone_name, account_id, tunnel_id, tunnel_token, healthy) == (
        "example.com",
        "acct-1",
        "tun-1",
        "tunnel-token",
        True,
    )


def test_cloudflare_client_reuses_remote_tunnel_and_refuses_local_ones():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/client/v4")
        if path == "/zones":
            # Deepest suffix wins because candidates run longest-first.
            name = request.url.params.get("name")
            if name == "home.example.com":
                return _ok(
                    [
                        {
                            "id": "zone-long",
                            "name": "home.example.com",
                            "account": {"id": "acct-1"},
                        }
                    ]
                )
            return _ok([])
        if path == "/accounts/acct-1/cfd_tunnel" and request.method == "GET":
            name = request.url.params.get("name")
            if name == "remote-tunnel":
                return _ok([{"id": "tun-9", "config_src": "cloudflare"}])
            if name == "local-tunnel":
                return _ok([{"id": "tun-8", "config_src": "local"}])
            return _ok([])
        if path == "/accounts/acct-1/cfd_tunnel/tun-9/token":
            return _ok("existing-token")
        raise AssertionError(f"unexpected call {request.method} {path}")

    async def drive():
        client = ea.CloudflareTunnelClient("tok", transport=_cf_transport(handler))
        try:
            zone = await client.find_zone("nymeria.home.example.com")
            reused = await client.get_or_create_tunnel("acct-1", "remote-tunnel")
            with pytest.raises(ea.CloudflareError, match="locally managed"):
                await client.get_or_create_tunnel("acct-1", "local-tunnel")
        finally:
            await client.aclose()
        return zone, reused

    zone, reused = asyncio.run(drive())
    assert zone == ("zone-long", "home.example.com", "acct-1")
    assert reused == ("tun-9", "existing-token")


def test_cloudflare_client_surfaces_api_error_messages():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"code": 9109, "message": "Invalid access token"}],
            },
        )

    async def drive():
        client = ea.CloudflareTunnelClient("bad", transport=_cf_transport(handler))
        try:
            await client.verify_token()
        finally:
            await client.aclose()

    with pytest.raises(ea.CloudflareError, match="Invalid access token"):
        asyncio.run(drive())


# --- public URL verification -------------------------------------------------------


def _health_transport(status_code: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(status_code, json={"status": "ok"})

    return httpx.MockTransport(handler)


def test_check_public_health_classifies_answers():
    healthy = asyncio.run(
        ea.check_public_health("https://x.example", transport=_health_transport(200))
    )
    assert healthy.status == "healthy" and healthy.tunnel_reached

    origin_down = asyncio.run(
        ea.check_public_health("https://x.example", transport=_health_transport(503))
    )
    assert origin_down.status == "origin_down" and origin_down.tunnel_reached

    # 530 wraps Cloudflare 1033 "cannot reach the tunnel": the CONNECTOR is
    # down, the opposite of origin-down, and must not count as tunnel-reached.
    connector_down = asyncio.run(
        ea.check_public_health("https://x.example", transport=_health_transport(530))
    )
    assert connector_down.status == "error" and not connector_down.tunnel_reached
    assert "connector" in connector_down.detail

    error = asyncio.run(
        ea.check_public_health("https://x.example", transport=_health_transport(404))
    )
    assert error.status == "error" and not error.tunnel_reached

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    unreachable = asyncio.run(
        ea.check_public_health(
            "https://x.example", transport=httpx.MockTransport(boom)
        )
    )
    assert unreachable.status == "unreachable" and not unreachable.tunnel_reached


def test_check_public_sse_flags_buffered_burst():
    # A MockTransport delivers the whole body at once: exactly what a relay
    # that buffers SSE looks like to the probe, so it must NOT pass.
    body = "".join(f"data: {{\"seq\": {i}}}\n\n" for i in range(3))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/stream"
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    probe = asyncio.run(
        ea.check_public_sse("https://x.example", transport=httpx.MockTransport(handler))
    )
    assert probe.events == 3
    assert not probe.ok and not probe.streamed
    assert "burst" in probe.detail


def test_check_public_sse_rejects_non_sse_content_type():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="hello", headers={"content-type": "text/html"})

    probe = asyncio.run(
        ea.check_public_sse("https://x.example", transport=httpx.MockTransport(handler))
    )
    assert not probe.ok and "content-type" in probe.detail


def test_check_public_sse_accepts_a_real_spread_stream():
    # A real socket server that writes events with gaps proves the streamed
    # path end to end (MockTransport cannot model incremental delivery).
    async def drive():
        async def handle(reader, writer):
            while (await reader.readline()).strip():
                pass  # drain request headers
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"content-type: text/event-stream\r\n"
                b"connection: close\r\n\r\n"
            )
            await writer.drain()
            for seq in range(3):
                if seq:
                    await asyncio.sleep(0.45)
                writer.write(f'data: {{"seq": {seq}}}\n\n'.encode())
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await ea.check_public_sse(f"http://127.0.0.1:{port}")
        finally:
            server.close()
            await server.wait_closed()

    probe = asyncio.run(drive())
    assert probe.ok and probe.streamed and probe.events == 3


# --- /health/stream endpoint ----------------------------------------------------


def test_health_stream_endpoint_emits_spaced_events():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nymeria.api.routers.system import create_system_router

    app = FastAPI()
    app.include_router(
        create_system_router(
            verify_api_key=lambda: None,
            require_admin_user=lambda: None,
            get_agent_fn=lambda: None,
            get_settings_fn=lambda: None,
        )
    )
    client = TestClient(app)
    with client.stream("GET", "/health/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    data_lines = [line for line in body.splitlines() if line.startswith("data:")]
    assert len(data_lines) >= 3
    assert "event: end" in body
    first = json.loads(data_lines[0].removeprefix("data:"))
    assert first["seq"] == 0 and "ts" in first


# --- finalize env writes ----------------------------------------------------------


def test_noninteractive_writes_external_access_env(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--external-access", "tailscale",
            "--public-url", "https://nym.tail42.ts.net/",
            "--root", str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert "NYMERIA_EXTERNAL_ACCESS=tailscale" in config
    assert "NYMERIA_PUBLIC_URL=https://nym.tail42.ts.net" in config
    cors = re.search(r"CORS_ORIGINS=(.*)", config)
    assert cors is not None
    origins = cors.group(1).strip().strip('"').split(",")
    assert "https://nym.tail42.ts.net" in origins
    assert "tauri://localhost" in origins  # defaults kept, origin appended


def test_noninteractive_external_access_without_url_writes_marker_only(
    monkeypatch, tmp_path
):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    rc = setup_main(
        [
            "--provider", "anthropic",
            "--model", "claude-test-model",
            "--api-key", "sk-ant-test-key",
            "--external-access", "chat_bots",
            "--root", str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert rc == 0
    assert "NYMERIA_EXTERNAL_ACCESS=chat_bots" in config
    assert "NYMERIA_PUBLIC_URL" not in config
    assert "CORS_ORIGINS" not in config


# --- hydrate round-trip -------------------------------------------------------------


def test_hydrate_recovers_external_access_choice_and_url(monkeypatch, tmp_path):
    from nymeria.onboarding import ExternalAccess
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    assert (
        setup_main(
            [
                "--provider", "anthropic",
                "--model", "claude-test-model",
                "--api-key", "sk-ant-test-key",
                "--external-access", "tailscale",
                "--public-url", "https://nym.tail42.ts.net",
                "--root", str(root),
                "--non-interactive",
            ]
        )
        == 0
    )

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state)
    assert state.reconfigure
    assert state.external_access is ExternalAccess.TAILSCALE
    assert state.external_access_recorded
    assert state.public_url == "https://nym.tail42.ts.net"
    assert "https://nym.tail42.ts.net" in state.existing_cors_origins

    # An explicit --external-access flag that DIFFERS from the recorded
    # choice is a switch: the old choice's URL must not ride along under the
    # new marker (the flag wins before hydrate, so the interactive clearing
    # in store_external_access_choice never sees the change).
    switched = WizardState(root=root, external_access=ExternalAccess.CHAT_BOTS)
    assert hydrate_state_from_disk(switched)
    assert switched.external_access is ExternalAccess.CHAT_BOTS
    assert switched.external_access_recorded
    assert switched.public_url == ""


def test_hydrate_ignores_invalid_external_access_marker(tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    root = tmp_path / "runtime"
    root.mkdir(parents=True)
    (root / "config.env").write_text(
        "NYMERIA_EXTERNAL_ACCESS=carrier-pigeon\n", encoding="utf-8"
    )

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state)
    assert state.external_access is None
    assert not state.external_access_recorded


def test_hand_set_public_url_without_marker_is_never_dropped(tmp_path):
    # Pre-marker installs were told to set NYMERIA_PUBLIC_URL by hand (Caddy,
    # existing tunnel). An unrelated reconfigure that Enters through the
    # local-only default must not retire the operator's line.
    from nymeria.setup.finalize import should_drop_public_url
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.onboarding import ExternalAccess

    root = tmp_path / "runtime"
    root.mkdir(parents=True)
    (root / "config.env").write_text(
        "NYMERIA_PUBLIC_URL=https://nymeria.example.com\n", encoding="utf-8"
    )

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state)
    assert state.public_url == "https://nymeria.example.com"
    assert not state.external_access_recorded
    # The choice step's local-only default gets stored, gating the URL out of
    # this run's writes, but the drop must NOT fire (the wizard does not own
    # that line).
    state.external_access = ExternalAccess.LOCAL_ONLY
    assert not should_drop_public_url(state)

    # A wizard-written URL (marker present) IS retired on a local-only switch.
    owned = WizardState(
        external_access=ExternalAccess.LOCAL_ONLY,
        external_access_recorded=True,
        public_url="https://nym.tail42.ts.net",
    )
    assert should_drop_public_url(owned)
    owned.external_access = ExternalAccess.TAILSCALE
    assert not should_drop_public_url(owned)  # active URL: keep writing it


def test_health_stream_spacing_exceeds_probe_threshold():
    # The endpoint's emission window and the probe's spread threshold live in
    # different modules; this pins their relationship so neither can drift
    # into flagging every transparent relay as buffering.
    from nymeria.api.routers.system import (
        HEALTH_STREAM_EVENT_COUNT,
        HEALTH_STREAM_INTERVAL_SECONDS,
    )

    emission_window = (HEALTH_STREAM_EVENT_COUNT - 1) * HEALTH_STREAM_INTERVAL_SECONDS
    assert emission_window >= 2 * ea.SSE_MIN_SPREAD_SECONDS


# --- choice switching and env retirement -------------------------------------------


def test_store_external_access_choice_clears_derived_state_on_switch():
    from nymeria.onboarding import ExternalAccess
    from nymeria.setup.steps.deployment import store_external_access_choice

    state = WizardState()
    store_external_access_choice(state, ExternalAccess.TAILSCALE)
    state.public_url = "https://nym.tail42.ts.net"
    state.public_url_verified = True
    state.tailscale_exposure = "serve"

    # Re-storing the same choice keeps the setup outputs.
    store_external_access_choice(state, ExternalAccess.TAILSCALE)
    assert state.public_url == "https://nym.tail42.ts.net"

    # Switching retires them: finalize writes whatever public_url holds.
    store_external_access_choice(state, ExternalAccess.LOCAL_ONLY)
    assert state.public_url == ""
    assert not state.public_url_verified
    assert state.tailscale_exposure == ""


def test_resolve_extra_env_gates_public_url_on_the_choice():
    from nymeria.onboarding import ExternalAccess
    from nymeria.setup.finalize import _resolve_extra_env

    # A stale hydrated URL must not be re-written under a local-only choice.
    state = WizardState(
        external_access=ExternalAccess.LOCAL_ONLY,
        public_url="https://nym.tail42.ts.net",
    )
    extra = _resolve_extra_env(state)
    assert extra["NYMERIA_EXTERNAL_ACCESS"] == "local_only"
    assert "NYMERIA_PUBLIC_URL" not in extra
    assert "CORS_ORIGINS" not in extra

    # Chat-bots keeps the URL: webhook bot platforms need NYMERIA_PUBLIC_URL.
    state.external_access = ExternalAccess.CHAT_BOTS
    extra = _resolve_extra_env(state)
    assert extra["NYMERIA_PUBLIC_URL"] == "https://nym.tail42.ts.net"


def test_local_only_rerun_retires_the_public_url_line(monkeypatch, tmp_path):
    from nymeria.setup import finalize as finalize_mod

    config = tmp_path / "config.env"
    finalize_mod.write_config(
        config,
        data_dir=tmp_path / "data",
        extra_env={
            "NYMERIA_EXTERNAL_ACCESS": "tailscale",
            "NYMERIA_PUBLIC_URL": "https://nym.tail42.ts.net",
        },
    )
    assert "NYMERIA_PUBLIC_URL" in config.read_text(encoding="utf-8")

    # The wizard switched to local-only: the merge write must retire the URL
    # line (or hydrate would resurrect it on every later reconfigure).
    finalize_mod.write_config(
        config,
        data_dir=tmp_path / "data",
        extra_env={"NYMERIA_EXTERNAL_ACCESS": "local_only"},
        merge=True,
        drop_public_url=True,
    )
    text = config.read_text(encoding="utf-8")
    assert "NYMERIA_PUBLIC_URL" not in text
    assert "NYMERIA_EXTERNAL_ACCESS=local_only" in text


def test_noninteractive_rejects_public_url_without_scheme(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    with pytest.raises(SystemExit, match="public-url"):
        setup_main(
            [
                "--provider", "anthropic",
                "--model", "claude-test-model",
                "--api-key", "sk-ant-test-key",
                "--public-url", "nym.example.com",
                "--root", str(tmp_path / "runtime"),
                "--non-interactive",
            ]
        )


def test_noninteractive_rejects_public_url_with_local_only(monkeypatch, tmp_path):
    # The local-only gate would silently discard the validated URL; refuse
    # the contradictory pair instead.
    _stub_llm(monkeypatch)
    with pytest.raises(SystemExit, match="local_only"):
        setup_main(
            [
                "--provider", "anthropic",
                "--model", "claude-test-model",
                "--api-key", "sk-ant-test-key",
                "--external-access", "local_only",
                "--public-url", "https://nym.example.com",
                "--root", str(tmp_path / "runtime"),
                "--non-interactive",
            ]
        )


# --- wizard step wiring ----------------------------------------------------------


def test_tunnel_steps_apply_only_to_their_choice():
    from nymeria.onboarding import ExternalAccess
    from nymeria.setup.steps import build_default_steps

    steps = {step.id: step for step in build_default_steps()}
    ts = steps["external_access_tailscale"]
    cf = steps["external_access_cloudflare"]

    state = WizardState()
    assert not ts.applies(state) and not cf.applies(state)
    state.external_access = ExternalAccess.TAILSCALE
    assert ts.applies(state) and not cf.applies(state)
    state.external_access = ExternalAccess.CLOUDFLARE
    assert cf.applies(state) and not ts.applies(state)
    # The external-access unit STAYS in quick mode (remote access is one of
    # the quickstart tier's irreducible questions); the tunnel steps still
    # gate on the choice.
    state.quick = True
    assert cf.applies(state) and not ts.applies(state)
    state.external_access = ExternalAccess.LOCAL_ONLY
    assert not cf.applies(state) and not ts.applies(state)
