"""The server browser launcher (nymeria/server_browser.py).

Covers the pure pieces (platform map, UA derivation, manifest transform,
bake, rig identity) and the orchestration (install, configure, run, stop,
status, provision) through injected fetchers, runners, and exec hooks. No
network, no real Chrome, no service manager is touched. Behaviour numbers
(E14 etc.) refer to the plan's expected-behaviours list.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from nymeria import server_browser as sb
from nymeria.server_browser import (
    DEFAULT_LABEL,
    EXTENSION_ID,
    RigConfig,
    RigHome,
    ServerBrowserError,
    bake_config,
    build_chrome_argv,
    cft_platform,
    chrome_relative_binary,
    configure,
    fetch_extension_release,
    headful_user_agent,
    install,
    missing_library_hint,
    missing_shared_libraries,
    parse_chrome_version,
    probe_sandbox,
    provision,
    resolve_rig_home,
    resolve_stable_download,
    run_browser,
    service_exec_argv,
    service_spec,
    stage_extension,
    status,
    stop_browser,
    transform_manifest,
    verify_binary,
)

CHROME_VERSION_LINE = "Google Chrome for Testing 152.0.7977.64"
POSIX = os.name != "nt"


# --- helpers ---------------------------------------------------------------------


def completed(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), code, out, err)


class FakeRunner:
    """Answer `chrome --version`, the sandbox probe, `ldd`, and `ps` from scripts."""

    def __init__(self, *, version_ok=True, sandbox_ok=True, no_sandbox_ok=True, ldd="", ps=""):
        self.calls: list[list[str]] = []
        self.version_ok = version_ok
        self.sandbox_ok = sandbox_ok
        self.no_sandbox_ok = no_sandbox_ok
        self.ldd = ldd
        self.ps = ps

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        assert "env" in kwargs and kwargs["env"] is not None, "every spawn must pass an env"
        if argv[0] == "ldd":
            return completed(argv, 0, self.ldd)
        if argv[0] == "ps":
            return completed(argv, 0, self.ps)
        if "--version" in argv:
            if self.version_ok:
                return completed(argv, 0, CHROME_VERSION_LINE + "\n")
            return completed(argv, 127, "", "error while loading shared libraries: libnss3.so")
        return completed(argv, 0)

    def popen(self, argv, **kwargs):
        """The sandbox probe: a Chrome that either opens DevTools or dies with a reason."""
        argv = list(argv)
        self.calls.append(argv)
        assert "env" in kwargs and kwargs["env"] is not None, "every spawn must pass an env"
        assert kwargs.get("stderr") is subprocess.PIPE, "the probe reads stderr"
        ok = self.no_sandbox_ok if "--no-sandbox" in argv else self.sandbox_ok
        if ok:
            return FakeProc(["DevTools listening on ws://127.0.0.1:41000/devtools/browser/probe"])
        return FakeProc(
            ["[1:1:0909/000000.000000:FATAL:zygote_host_impl_linux.cc(1)] "
             "Failed to move to new namespace: Operation not permitted"],
            exit_code=1,
        )


class FakeProc:
    """What `FakeRunner.popen` hands the probe: stderr lines, then alive or exited."""

    def __init__(self, stderr_lines, *, exit_code=None):
        self.stderr = iter([(line + "\n").encode() for line in stderr_lines])
        self._exit_code = exit_code
        self.returncode = None
        self.pid = 2_000_000_000  # no such process; the tree stop must not need it
        self.stopped = False

    def poll(self):
        if self._exit_code is not None:
            self.returncode = self._exit_code
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._exit_code if self._exit_code is not None else -15
        return self.returncode

    def terminate(self):
        self.stopped = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()


def make_home(tmp_path: Path, *, with_binary: bool = True, platform_key: str = "linux64") -> RigHome:
    home = RigHome(tmp_path / "rig")
    if with_binary:
        binary = home.cft_dir / "152.0.7977.64" / chrome_relative_binary(platform_key)
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    return home


def make_build_dir(tmp_path: Path, *, version: str = "0.29.0", name: str = "Nymeria Browser") -> Path:
    build = tmp_path / "dist"
    build.mkdir(exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": name,
        "version": version,
        "key": "PINNED",
        "host_permissions": [],
        "optional_host_permissions": ["https://*/*", "http://localhost/*", "http://127.0.0.1/*"],
    }
    (build / "manifest.json").write_text(json.dumps(manifest))
    (build / "background.js").write_text("// sw\n")
    return build


def make_release_zip(tmp_path: Path, build: Path, *, wrapper: str = "nymeria-browser-v0.29.0") -> Path:
    zip_path = tmp_path / f"{wrapper}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in sorted(build.rglob("*")):
            if path.is_file():
                archive.write(path, f"{wrapper}/{path.relative_to(build)}")
    return zip_path


def sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- platform, UA, versions -------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", "linux64"),
        ("Darwin", "arm64", "mac-arm64"),
        ("Darwin", "x86_64", "mac-x64"),
        ("Windows", "AMD64", "win64"),
    ],
)
def test_cft_platform_map(system, machine, expected):
    assert cft_platform(system, machine) == expected


def test_cft_platform_rejects_unsupported():
    with pytest.raises(ServerBrowserError):
        cft_platform("Linux", "aarch64")
    with pytest.raises(ServerBrowserError):
        cft_platform("FreeBSD", "x86_64")


def test_chrome_relative_binary_per_platform():
    assert chrome_relative_binary("linux64") == Path("chrome-linux64/chrome")
    assert chrome_relative_binary("win64") == Path("chrome-win64/chrome.exe")
    mac = chrome_relative_binary("mac-arm64")
    assert mac.parts[0] == "chrome-mac-arm64" and mac.name == "Google Chrome for Testing"


def test_resolve_stable_download_picks_platform():
    payload = {
        "channels": {
            "Stable": {
                "version": "152.0.7977.64",
                "downloads": {
                    "chrome": [
                        {"platform": "linux64", "url": "https://x/linux64.zip"},
                        {"platform": "win64", "url": "https://x/win64.zip"},
                    ]
                },
            }
        }
    }
    assert resolve_stable_download(payload, "win64") == ("152.0.7977.64", "https://x/win64.zip")
    with pytest.raises(ServerBrowserError, match="no mac-arm64 download"):
        resolve_stable_download(payload, "mac-arm64")
    with pytest.raises(ServerBrowserError, match="unexpected shape"):
        resolve_stable_download({"channels": {}}, "linux64")


def test_headful_user_agent_never_says_headless_and_zeroes_minor():
    # E22: the product token is Chrome/<major>.0.0.0, never HeadlessChrome.
    ua = headful_user_agent(152, "linux64")
    assert "HeadlessChrome" not in ua
    assert "Chrome/152.0.0.0 Safari/537.36" in ua
    assert "X11; Linux x86_64" in ua
    assert "Windows NT 10.0; Win64; x64" in headful_user_agent(153, "win64")
    assert "Macintosh; Intel Mac OS X 10_15_7" in headful_user_agent(153, "mac-arm64")


def test_parse_chrome_version():
    assert parse_chrome_version(CHROME_VERSION_LINE) == "152.0.7977.64"
    with pytest.raises(ServerBrowserError):
        parse_chrome_version("no version here")


# --- rig home resolution -----------------------------------------------------------


def test_resolve_rig_home_precedence(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)
    assert resolve_rig_home(root).path == (root / "data" / "server-browser").resolve()
    (root / "config.env").write_text(f'{sb.HOME_ENV_KEY}="{tmp_path / "elsewhere"}"\n')
    assert resolve_rig_home(root).path == (tmp_path / "elsewhere").resolve()
    explicit = tmp_path / "explicit"
    assert resolve_rig_home(root, explicit).path == explicit.resolve()


def test_resolve_api_port_reads_env_file(tmp_path):
    root = tmp_path
    assert sb.resolve_api_port(root) == 8000
    (root / ".env").write_text("API_PORT=8010\n")
    assert sb.resolve_api_port(root) == 8010


# --- manifest transform and bake (E16) -----------------------------------------------


def test_transform_manifest_makes_optional_permissions_required():
    manifest = {
        "host_permissions": [],
        "optional_host_permissions": ["https://*/*", "http://localhost/*", "http://127.0.0.1/*"],
    }
    out = transform_manifest(manifest, "https://nymeria.example.com")
    assert out["host_permissions"] == ["https://*/*", "http://localhost/*", "http://127.0.0.1/*"]
    assert out["optional_host_permissions"] == []
    # The input is not mutated (the caller may still hold it).
    assert manifest["optional_host_permissions"]


def test_transform_manifest_adds_plain_http_origin_only_when_not_loopback():
    manifest = {"host_permissions": [], "optional_host_permissions": ["https://*/*"]}
    remote = transform_manifest(manifest, "http://nymeria-api:8000")
    assert "http://nymeria-api/*" in remote["host_permissions"]
    loopback = transform_manifest(manifest, "http://localhost:8010")
    assert loopback["host_permissions"] == ["https://*/*"]
    loopback_ip = transform_manifest(manifest, "http://127.0.0.1:8000")
    assert loopback_ip["host_permissions"] == ["https://*/*"]
    https = transform_manifest(manifest, "https://nymeria.example.com")
    assert https["host_permissions"] == ["https://*/*"]


def test_transform_manifest_is_idempotent():
    manifest = {"host_permissions": [], "optional_host_permissions": ["https://*/*"]}
    once = transform_manifest(manifest, "http://box:8000")
    twice = transform_manifest(once, "http://box:8000")
    assert once == twice


def test_bake_config_writes_identity_fields_mode_0600(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    path = bake_config(
        ext,
        base_url="http://localhost:8000/",
        token="nym_secret",
        client_id="nymeria-browser-abc",
        label="server browser",
    )
    data = json.loads(path.read_text())
    assert data == {
        "baseUrl": "http://localhost:8000",
        "token": "nym_secret",
        "clientId": "nymeria-browser-abc",
        "kind": "server",
        "label": "server browser",
    }
    if POSIX:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- extension staging and the pinned release (E21) -------------------------------------


def test_stage_extension_from_dir_and_zip(tmp_path):
    build = make_build_dir(tmp_path)
    ext = tmp_path / "ext"
    assert stage_extension(build, ext) == "0.29.0"
    assert (ext / "background.js").is_file()
    zip_path = make_release_zip(tmp_path, build)
    ext2 = tmp_path / "ext2"
    assert stage_extension(zip_path, ext2) == "0.29.0"
    assert json.loads((ext2 / "manifest.json").read_text())["name"] == "Nymeria Browser"


def test_stage_extension_rejects_foreign_or_missing_builds(tmp_path):
    other = make_build_dir(tmp_path, name="Some Other Extension")
    with pytest.raises(ServerBrowserError, match="not the Nymeria Browser"):
        stage_extension(other, tmp_path / "ext")
    with pytest.raises(ServerBrowserError, match="no extension build"):
        stage_extension(tmp_path / "missing", tmp_path / "ext")


def test_fetch_extension_release_refuses_digest_mismatch(tmp_path):
    home = RigHome(tmp_path / "rig")
    build = make_build_dir(tmp_path)
    zip_path = make_release_zip(tmp_path, build)
    payload = zip_path.read_bytes()

    def download(url, dest):
        assert url.endswith("/releases/download/v0.29.0/nymeria-browser-v0.29.0.zip")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)

    with pytest.raises(ServerBrowserError, match="did not match its pinned digest") as exc:
        fetch_extension_release(home, tag="v0.29.0", sha256="0" * 64, download=download)
    assert "expected " + "0" * 64 in str(exc.value)
    # The bad artifact is not left around to be staged later by hand.
    assert not (home.downloads_dir / "nymeria-browser-v0.29.0.zip").exists()
    good = fetch_extension_release(home, tag="v0.29.0", sha256=sha256_of(zip_path), download=download)
    assert good.is_file()


def test_fetch_extension_release_unpinned_reports_digest(tmp_path):
    home = RigHome(tmp_path / "rig")
    build = make_build_dir(tmp_path)
    zip_path = make_release_zip(tmp_path, build)
    logs: list[str] = []

    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zip_path.read_bytes())

    fetch_extension_release(home, tag="v0.29.0", sha256="", download=download, log=logs.append)
    assert any(sha256_of(zip_path) in line for line in logs)


# --- install (E14) ----------------------------------------------------------------------


def _cft_payload(url: str) -> bytes:
    return json.dumps(
        {
            "channels": {
                "Stable": {
                    "version": "152.0.7977.64",
                    "downloads": {"chrome": [{"platform": "linux64", "url": url}]},
                }
            }
        }
    ).encode()


def _cft_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("chrome-linux64/chrome")
        info.external_attr = (0o100755) << 16
        archive.writestr(info, "#!/bin/sh\necho chrome\n")
        archive.writestr("chrome-linux64/libfoo.so", "lib")
    return buf.getvalue()


def test_install_downloads_extracts_with_exec_bits_and_verifies(tmp_path):
    home = RigHome(tmp_path / "rig")
    runner = FakeRunner()
    downloads: list[str] = []

    def download(url, dest):
        downloads.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cft_zip_bytes())

    result = install(
        home,
        fetch=lambda url: _cft_payload("https://cft/linux64.zip"),
        download=download,
        run=runner,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert result.version == "152.0.7977.64" and not result.already_installed
    assert downloads == ["https://cft/linux64.zip"]
    binary = home.cft_dir / "152.0.7977.64" / "chrome-linux64" / "chrome"
    assert binary.is_file()
    if POSIX:
        assert binary.stat().st_mode & stat.S_IXUSR
    # The zip is not kept around.
    assert not list(home.downloads_dir.glob("cft-*.zip"))
    # A second install is a no-op that never downloads.
    again = install(
        home,
        fetch=lambda url: _cft_payload("https://cft/linux64.zip"),
        download=download,
        run=runner,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert again.already_installed and downloads == ["https://cft/linux64.zip"]


def test_verify_binary_names_missing_libraries(tmp_path, monkeypatch):
    # E14: a binary that cannot load names the packages to install.
    monkeypatch.setattr(sb.sys, "platform", "linux")
    runner = FakeRunner(
        version_ok=False,
        ldd=(
            "\tlinux-vdso.so.1 (0x00007ffd)\n"
            "\tlibnss3.so => not found\n"
            "\tlibatk-bridge-2.0.so.0 => not found\n"
            "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)\n"
        ),
    )
    with pytest.raises(ServerBrowserError) as exc:
        verify_binary(tmp_path / "chrome", run=runner)
    joined = "\n".join(exc.value.hints)
    assert "libnss3.so, libatk-bridge-2.0.so.0" in joined
    assert "apt-get install -y libatk-bridge2.0-0 libnss3" in joined


def test_missing_library_parsing_and_generic_hint():
    assert missing_shared_libraries("a => not found\n\tb.so.1 => not found\n\ta => not found\n") == [
        "a",
        "b.so.1",
    ]
    generic = missing_library_hint([])
    assert any("apt-get install" in line for line in generic)
    unknown = missing_library_hint(["libweird.so.9"])
    assert any("apt-file search" in line for line in unknown)


# --- sandbox probe and launch argv (E15, E22) ----------------------------------------------


def test_probe_sandbox_prefers_sandbox_and_falls_back_when_measured_failing(tmp_path):
    binary = tmp_path / "chrome"
    ok = probe_sandbox(binary, popen=FakeRunner(sandbox_ok=True).popen, log=lambda _: None)
    assert ok.no_sandbox is False and ok.reason == ""
    runner = FakeRunner(sandbox_ok=False, no_sandbox_ok=True)
    fallback = probe_sandbox(binary, run=runner, popen=runner.popen, log=lambda _: None)
    assert fallback.no_sandbox is True
    assert "Operation not permitted" in fallback.reason
    # Exactly two launches: one with the sandbox, one without, in that order.
    probes = [c for c in runner.calls if "--remote-debugging-port=0" in c]
    assert len(probes) == 2
    assert "--no-sandbox" not in probes[0] and "--no-sandbox" in probes[1]


def test_probe_sandbox_raises_when_even_no_sandbox_fails(tmp_path):
    with pytest.raises(ServerBrowserError, match="even without its sandbox"):
        probe_sandbox(
            tmp_path / "chrome",
            popen=FakeRunner(sandbox_ok=False, no_sandbox_ok=False).popen,
            log=lambda _: None,
        )


def test_build_chrome_argv_never_enables_automation_and_gates_no_sandbox(tmp_path):
    def argv_for(no_sandbox: bool) -> list[str]:
        return build_chrome_argv(
            tmp_path / "chrome",
            profile_dir=tmp_path / "profile",
            ext_dir=tmp_path / "ext",
            debug_port=9333,
            user_agent="UA",
            no_sandbox=no_sandbox,
        )

    with_sandbox = argv_for(False)
    without = argv_for(True)
    for argv in (with_sandbox, without):
        assert "--enable-automation" not in argv
        assert not any(a.startswith("--remote-debugging-address") for a in argv)
        assert "--headless=new" in argv
        assert "--user-agent=UA" in argv
        assert "--remote-debugging-port=9333" in argv
        assert f"--load-extension={tmp_path / 'ext'}" in argv
    assert "--no-sandbox" not in with_sandbox
    assert "--no-sandbox" in without


def test_build_chrome_argv_allows_only_the_desktop_devtools_origin(tmp_path):
    # The origin allowlist and the loopback bind are separate controls: this
    # one stops WEB CONTENT on this host attaching to the debugger of a
    # browser holding live signed-in sessions. Adding a web origin back is a
    # deliberate act, not a drive-by edit.
    argv = build_chrome_argv(
        tmp_path / "chrome",
        profile_dir=tmp_path / "profile",
        ext_dir=tmp_path / "ext",
        debug_port=9222,
        user_agent="UA",
        no_sandbox=False,
    )
    origins = [a for a in argv if a.startswith("--remote-allow-origins")]
    assert origins == ["--remote-allow-origins=devtools://devtools"]
    assert not any("http://" in o or "https://" in o for o in origins)


# --- configure ------------------------------------------------------------------------------


def test_configure_stages_bakes_and_keeps_identity_across_reruns(tmp_path):
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    runner = FakeRunner(sandbox_ok=False, no_sandbox_ok=True)
    first = configure(
        home,
        base_url="http://localhost:8010/",
        token="tok1",
        source=build,
        debug_port=9300,
        run=runner,
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert first.client_id.startswith("nymeria-browser-")
    assert first.label == DEFAULT_LABEL and first.kind == "server"
    assert first.debug_port == 9300 and first.no_sandbox is True
    assert first.extension_version == "0.29.0" and first.cft_version == "152.0.7977.64"
    baked = json.loads(home.baked_config.read_text())
    assert baked["clientId"] == first.client_id and baked["token"] == "tok1"
    assert baked["baseUrl"] == "http://localhost:8010"
    manifest = json.loads((home.ext_dir / "manifest.json").read_text())
    assert manifest["optional_host_permissions"] == []
    assert "https://*/*" in manifest["host_permissions"]
    if POSIX:
        assert stat.S_IMODE(home.profile_dir.stat().st_mode) == 0o700
    # Re-configure with a rotated token: same identity, same port, new token.
    second = configure(
        home,
        base_url="http://localhost:8010",
        token="tok2",
        source=build,
        sandbox="on",
        run=runner,
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert second.client_id == first.client_id
    assert second.debug_port == 9300
    assert second.no_sandbox is False
    assert json.loads(home.baked_config.read_text())["token"] == "tok2"
    reloaded = RigConfig.load(home)
    assert reloaded is not None and reloaded.client_id == first.client_id


def test_configure_pins_the_rig_home_and_bake_modes(tmp_path):
    # The home is 0700 and the bake 0600: the profile under it holds every
    # signed-in site session and the bake carries an account token.
    home = make_home(tmp_path)
    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=make_build_dir(tmp_path),
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    if not POSIX:
        pytest.skip("POSIX modes")
    assert stat.S_IMODE(home.path.stat().st_mode) == 0o700
    assert stat.S_IMODE(home.profile_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(home.baked_config.stat().st_mode) == 0o600
    # ext/ itself is readable, which is safe only because the 0700 home
    # encloses it and the one secret inside is the 0600 bake.
    assert home.ext_dir.parent == home.path


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_enforce_mode_reports_a_mode_that_did_not_stick(tmp_path, monkeypatch):
    target = tmp_path / "dir"
    target.mkdir(mode=0o755)
    assert sb.enforce_mode(target, 0o700) == ""
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    # A filesystem that accepts chmod and keeps its own mode anyway (CIFS).
    target.chmod(0o755)
    monkeypatch.setattr(sb.os, "chmod", lambda *a, **k: None)
    assert "kept 0755 instead of 0700" in sb.enforce_mode(target, 0o700)


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_configure_warns_instead_of_claiming_a_mode_it_did_not_get(tmp_path, monkeypatch):
    # The docstring, the deployment doc and the success line all promise
    # 0700/0600; on a filesystem that refuses them the operator must be told,
    # not reassured. The profile holds every signed-in site session.
    home = make_home(tmp_path)
    logs: list[str] = []

    real_chmod = os.chmod

    def refuse(path, mode, *args, **kwargs):
        # Only the hardening modes: copytree's own copystat must still work.
        if mode in (0o700, 0o600):
            raise OSError(1, "Operation not permitted")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(sb.os, "chmod", refuse)
    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=make_build_dir(tmp_path),
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=logs.append,
        platform_key="linux64",
    )
    joined = "\n".join(logs)
    assert "rig home" in joined and "is not restricted to 0700" in joined
    assert "signed-in site sessions" in joined
    assert "the baked extension config" in joined and "is not restricted to 0600" in joined
    # The summary line quotes the mode the file ACTUALLY has, never a literal.
    actual = f"{stat.S_IMODE(home.baked_config.stat().st_mode):04o}"
    assert f"baked config ({actual})" in joined


def test_configure_refuses_an_unreadable_rig_json_instead_of_minting_a_new_identity(tmp_path):
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    first = configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=build,
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    home.rig_json.write_text('{"client_id": "nymeria-brow')  # truncated write
    with pytest.raises(ServerBrowserError, match="cannot be read") as exc:
        configure(
            home,
            base_url="http://localhost:8000",
            token="t",
            source=build,
            sandbox="on",
            run=(runner := FakeRunner()),
            popen=runner.popen,
            log=lambda _: None,
            platform_key="linux64",
        )
    assert any("/browser default" in hint for hint in exc.value.hints)
    # The old identity is still on disk: nothing re-identified the browser.
    assert first.client_id.startswith("nymeria-browser-")
    assert RigConfig.load(RigHome(tmp_path / "nothing")) is None


def test_rig_json_and_bake_are_written_atomically(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    config = RigConfig(client_id="nymeria-browser-a", base_url="http://localhost:8000")
    config.save(home)
    ext = home.ext_dir
    ext.mkdir(parents=True, exist_ok=True)
    bake_config(ext, base_url="http://x", token="t1", client_id="nymeria-browser-a")
    seen: list[str] = []
    tmp_modes: list[int] = []
    real_replace = os.replace

    def watching_replace(src, dst):
        # At the moment of the swap the destination still holds the OLD,
        # complete content: no reader ever sees a half-written file. The
        # temp file already carries its final mode, so the bake is never
        # briefly world-readable either.
        seen.append(Path(dst).read_text())
        tmp_modes.append(stat.S_IMODE(Path(src).stat().st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(sb.os, "replace", watching_replace)
    RigConfig(client_id="nymeria-browser-a", base_url="http://localhost:9999").save(home)
    bake_config(ext, base_url="http://y", token="t2", client_id="nymeria-browser-a")
    assert json.loads(seen[0])["base_url"] == "http://localhost:8000"
    assert json.loads(seen[1])["token"] == "t1"
    assert json.loads(home.rig_json.read_text())["base_url"] == "http://localhost:9999"
    assert json.loads(home.baked_config.read_text())["token"] == "t2"
    if POSIX:
        # The mode survives the rename, and the temp file never widened it.
        assert stat.S_IMODE(home.baked_config.stat().st_mode) == 0o600
        assert tmp_modes[1] == 0o600
    assert not list(home.path.glob(".*tmp"))
    assert not list(ext.glob(".*tmp"))


def test_configure_restarts_a_service_installed_rig_so_the_new_bake_is_adopted(tmp_path, monkeypatch):
    # The extension adopts the bake at worker BOOTSTRAP and the SSE fetch keeps
    # that worker alive, so a re-bake under a running browser is a no-op until
    # something restarts it.
    home = _configured_home(tmp_path)
    logs: list[str] = []
    stopped: list[str] = []
    restarted: list[str] = []

    class FakeManager:
        def restart(self):
            restarted.append("restart")

    monkeypatch.setattr(sb, "running_pid", lambda h, run=None: 4242)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    monkeypatch.setattr(
        sb, "stop_browser", lambda h, **k: stopped.append("stop") or ["Stopped the server browser (pid 4242)."]
    )
    monkeypatch.setattr(sb, "_browser_service_for", lambda port, runner=None: FakeManager())
    configure(
        home,
        base_url="http://localhost:8000",
        token="rotated",
        source=make_build_dir(tmp_path),
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=logs.append,
        platform_key="linux64",
    )
    assert stopped == ["stop"], "the extension must not be rewritten under a running browser"
    assert restarted == ["restart"]
    assert any("Restarted the server browser service" in line for line in logs)


def test_configure_without_a_service_says_loudly_how_to_start_the_rig_again(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    logs: list[str] = []
    monkeypatch.setattr(sb, "running_pid", lambda h, run=None: 4242)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    monkeypatch.setattr(sb, "stop_browser", lambda h, **k: ["Stopped the server browser (pid 4242)."])
    monkeypatch.setattr(sb, "_browser_service_for", lambda port, runner=None: None)
    configure(
        home,
        base_url="http://localhost:8000",
        token="rotated",
        source=make_build_dir(tmp_path),
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=logs.append,
        platform_key="linux64",
    )
    joined = "\n".join(logs)
    assert "is NOT running again" in joined
    assert f"nymeria browser run --home {home.path}" in joined


def test_configure_retires_the_service_unit_the_old_debug_port_owned(tmp_path, monkeypatch):
    # Unit names are keyed on the debug port, so a port change would otherwise
    # leave two units driving one profile, the loser burning its restart budget.
    home = _configured_home(tmp_path)  # configured on 9400
    logs: list[str] = []
    uninstalled: list[int] = []

    class FakeManager:
        def __init__(self, port):
            self.port = port

        def uninstall(self):
            uninstalled.append(self.port)
            return (f"Removed systemd user unit: nymeria-browser-{self.port}.service",)

    monkeypatch.setattr(sb, "_browser_service_for", lambda port, runner=None: FakeManager(port))
    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=make_build_dir(tmp_path),
        debug_port=9401,
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=logs.append,
        platform_key="linux64",
    )
    assert uninstalled == [9400]
    assert any("nymeria browser service install" in line for line in logs)
    # Same port: nothing is retired.
    uninstalled.clear()
    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=make_build_dir(tmp_path),
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=logs.append,
        platform_key="linux64",
    )
    assert uninstalled == []


def test_configure_requires_install_and_token(tmp_path):
    home = make_home(tmp_path, with_binary=False)
    with pytest.raises(ServerBrowserError, match="not installed"):
        configure(home, base_url="http://localhost:8000", token="t", source=tmp_path, platform_key="linux64")
    home2 = make_home(tmp_path / "b")
    with pytest.raises(ServerBrowserError, match="base URL and an account token"):
        configure(home2, base_url="http://localhost:8000", token="  ", source=tmp_path, platform_key="linux64")


def test_configure_rejects_bad_client_id(tmp_path):
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    with pytest.raises(ServerBrowserError, match="must start with"):
        configure(
            home,
            base_url="http://localhost:8000",
            token="t",
            client_id="desktop-123",
            source=build,
            sandbox="on",
            run=(runner := FakeRunner()),
            popen=runner.popen,
            platform_key="linux64",
        )


def test_configure_adopts_an_existing_profile(tmp_path):
    # E20: the old rig's signed-in sessions ride along.
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    old = tmp_path / "old-rig"
    (old / "profile" / "Default").mkdir(parents=True)
    (old / "profile" / "Default" / "Cookies").write_text("session-bytes")
    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=build,
        sandbox="on",
        adopt_home=old,
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert (home.profile_dir / "Default" / "Cookies").read_text() == "session-bytes"
    # The original is left alone.
    assert (old / "profile" / "Default" / "Cookies").exists()


# --- run and stop (E17) -------------------------------------------------------------------------


def _configured_home(tmp_path) -> RigHome:
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=build,
        sandbox="on",
        debug_port=9400,
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    return home


@pytest.mark.skipif(not POSIX, reason="exec path is POSIX-only")
def test_run_browser_holds_the_guard_and_execs_chrome_with_a_scrubbed_env(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    execs: list[tuple[str, list[str], dict]] = []
    held: list[str] = []
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", "master-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-provider")

    def exec_fn(path, argv, env):
        # The guard names US at the moment of exec, because exec keeps our pid.
        held.append(home.pid_file.read_text().strip())
        execs.append((path, list(argv), dict(env)))

    run_browser(
        home,
        run=FakeRunner(),
        exec_fn=exec_fn,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert len(execs) == 1
    path, argv, env = execs[0]
    assert path == argv[0] and argv[0].endswith("chrome")
    assert "--user-agent=" + headful_user_agent(152, "linux64") in argv
    assert "--remote-debugging-port=9400" in argv
    assert "--enable-automation" not in argv
    assert held == [str(os.getpid())]
    # The environment Chrome is handed is the scrubbed one, not run.py's, which
    # by this point holds the vault key and every provider key.
    assert "NYMERIA_SECRETS_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert env.get("PATH") == os.environ["PATH"]


def test_run_browser_refuses_while_an_instance_is_alive(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    home.pid_file.write_text("4242\n")
    monkeypatch.setattr(sb, "pid_alive", lambda pid: pid == 4242)
    with pytest.raises(ServerBrowserError, match="already running \\(pid 4242\\)"):
        run_browser(home, run=FakeRunner(), exec_fn=lambda *a: None, platform_key="linux64")


def test_run_browser_refuses_when_a_stray_uses_the_profile(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [777])
    with pytest.raises(ServerBrowserError, match="pid 777"):
        run_browser(home, run=FakeRunner(), exec_fn=lambda *a: None, platform_key="linux64")


def test_run_browser_fresh_profile_wipes_then_recreates(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    (home.profile_dir / "Default").mkdir()
    (home.profile_dir / "Default" / "Cookies").write_text("old")
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    run_browser(
        home,
        fresh_profile=True,
        run=FakeRunner(),
        exec_fn=lambda *a: None,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert home.profile_dir.is_dir() and not (home.profile_dir / "Default").exists()


def test_run_browser_supervise_restarts_on_abnormal_exit(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    monkeypatch.setattr(sb.time, "sleep", lambda s: None)
    exits = iter([3, 3, 0])
    spawned: list[int] = []

    class FakeChild:
        def __init__(self, pid):
            self.pid = pid

        def wait(self):
            return next(exits)

    def popen(argv, env=None) -> Any:
        assert env is not None
        spawned.append(len(spawned) + 1)
        return FakeChild(1000 + len(spawned))

    code = run_browser(
        home,
        supervise=True,
        run=FakeRunner(),
        popen=popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert code == 0 and spawned == [1, 2, 3]
    assert not home.pid_file.exists()


def _supervise(home, exits, **kwargs):
    """Drive the supervise loop over a scripted list of Chrome exit codes."""
    codes = iter(exits)
    spawned: list[int] = []

    class FakeChild:
        def __init__(self, pid):
            self.pid = pid

        def wait(self):
            return next(codes)

    def popen(argv, env=None) -> Any:
        assert env is not None
        spawned.append(len(spawned) + 1)
        return FakeChild(1000 + len(spawned))

    code = run_browser(
        home,
        supervise=True,
        run=FakeRunner(),
        popen=popen,
        log=lambda _: None,
        platform_key="linux64",
        **kwargs,
    )
    return code, spawned


def test_run_browser_supervise_stays_down_after_a_stop_request(tmp_path, monkeypatch):
    # Killing Chrome under a supervisor is not a stop: it sees a non-zero exit
    # and starts another one while `stop` reports success.
    home = _configured_home(tmp_path)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    monkeypatch.setattr(sb.time, "sleep", lambda s: None)

    class FakeChild:
        pid = 1234

        def wait(self):
            sb.request_stop(home)  # a `stop` lands while Chrome is dying
            return 143  # SIGTERM: the exit code that used to trigger a restart

    spawned: list[int] = []

    def popen(argv, env=None) -> Any:
        spawned.append(1)
        return FakeChild()

    code = run_browser(
        home,
        supervise=True,
        run=FakeRunner(),
        popen=popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert code == 0
    assert spawned == [1], "a stopped rig must not be restarted by its own supervisor"


def test_run_browser_clears_a_stale_stop_request_before_launching(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    sb.request_stop(home)
    code, spawned = _supervise(home, [0])
    assert code == 0 and spawned == [1]
    assert not sb.stop_requested(home)


def test_run_browser_restart_budget_resets_after_a_healthy_run(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    monkeypatch.setattr(sb.time, "sleep", lambda s: None)
    # A crash loop (nothing stays up) exhausts the budget and gives up.
    code, spawned = _supervise(home, [3, 3, 3], max_restarts=2)
    assert code == 3 and spawned == [1, 2, 3]
    # The same three failures, each after a run that stayed healthy, do not:
    # a long-lived service must not spend its budget on unrelated crashes.
    code, spawned = _supervise(home, [3, 3, 3, 0], max_restarts=2, healthy_run_seconds=0.0)
    assert code == 0 and spawned == [1, 2, 3, 4]


@pytest.mark.skipif(not POSIX, reason="ps scan is POSIX-only")
def test_stray_chrome_pids_needs_this_rigs_binary_not_just_the_profile_path(tmp_path):
    # The predicate decides what `stop` SIGKILLs, so it anchors on argv[0]
    # being this rig's own Chrome. A process that merely mentions the profile
    # path (a grep, an editor, a backup job) is not a browser.
    home = make_home(tmp_path)
    chrome = str(home.chrome_binary("linux64"))
    profile = home.profile_dir
    other = RigHome(tmp_path / "other-rig")
    ps = (
        f"  100 {chrome} --headless=new --user-data-dir={profile} --remote-debugging-port=9222\n"
        f"  101 {chrome} --type=renderer --user-data-dir={profile}\n"
        f"  102 {chrome} --headless=new --user-data-dir={other.profile_dir}\n"
        f"  103 grep --user-data-dir={profile}\n"
        f"  104 /usr/bin/chrome --headless=new --user-data-dir={profile}\n"
        f"  105 vim {chrome}-notes --user-data-dir={profile}\n"
    )
    assert sb.stray_chrome_pids(home, run=FakeRunner(ps=ps)) == [100]


def _ps_for_pid(cmdline: str):
    """A runner that answers `ps -o args= -p <pid>` with ``cmdline``."""

    def run(argv, **kwargs):
        assert kwargs.get("env") is not None, "every spawn must pass an env"
        assert argv[:2] == ["ps", "-o"]
        return completed(argv, 0, cmdline + "\n")

    return run


@pytest.mark.skipif(not POSIX, reason="ps scan is POSIX-only")
def test_running_pid_refuses_a_recycled_pid_that_is_not_this_rigs_browser(tmp_path):
    # A guard file survives a reboot or a SIGKILL naming a dead pid, and the
    # kernel hands that number out again: on this host to the API, the worker
    # or postgres. `stop` must not signal it.
    home = _configured_home(tmp_path)
    chrome = str(home.chrome_binary("linux64"))
    home.pid_file.write_text(f"{os.getpid()}\n")
    assert sb.running_pid(home, run=_ps_for_pid("postgres: checkpointer")) is None
    ours = f"{chrome} --headless=new --user-data-dir={home.profile_dir}"
    assert sb.running_pid(home, run=_ps_for_pid(ours)) == os.getpid()


@pytest.mark.skipif(not POSIX, reason="ps scan is POSIX-only")
def test_stop_browser_never_signals_a_pid_that_is_not_this_rigs_browser(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    home.pid_file.write_text("500\n")
    signalled: list[int] = []
    monkeypatch.setattr(sb, "pid_alive", lambda pid: True)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    monkeypatch.setattr(sb, "_terminate", lambda pid: signalled.append(pid))
    monkeypatch.setattr(sb, "_kill", lambda pid: signalled.append(pid))
    lines = stop_browser(home, run=_ps_for_pid("/usr/lib/postgresql/16/bin/postgres -D /var/lib"))
    assert signalled == []
    assert lines[0].startswith("No server browser")
    assert not home.pid_file.exists()


def test_terminate_and_kill_survive_a_pid_owned_by_another_user(monkeypatch):
    def deny(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(sb.os, "kill", deny)
    monkeypatch.setattr(sb.os, "name", "posix")
    sb._terminate(4242)
    sb._kill(4242)


@pytest.mark.skipif(not POSIX, reason="O_EXCL guard is exercised on POSIX")
def test_pid_guard_is_atomic_and_replaces_only_a_dead_holder(tmp_path, monkeypatch):
    home = RigHome(tmp_path / "rig")
    sb._claim_pid_file(home, os.getpid())
    assert home.pid_file.read_text().strip() == str(os.getpid())
    # A second claim while the holder lives refuses rather than overwriting.
    with pytest.raises(ServerBrowserError, match="already running"):
        sb._claim_pid_file(home, 999)
    assert home.pid_file.read_text().strip() == str(os.getpid())
    # A dead holder is stale and is taken over.
    monkeypatch.setattr(sb, "pid_alive", lambda pid: False)
    sb._claim_pid_file(home, 999)
    assert home.pid_file.read_text().strip() == "999"


def test_stop_browser_terminates_pidfile_and_strays(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    home.pid_file.write_text("500\n")
    alive = {500, 600}
    terminated: list[int] = []
    killed: list[int] = []
    monkeypatch.setattr(sb, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(sb, "running_pid", lambda h, run=None: 500)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [600])

    def terminate(pid):
        terminated.append(pid)
        if pid == 500:
            alive.discard(pid)

    def kill(pid):
        killed.append(pid)
        alive.discard(pid)

    monkeypatch.setattr(sb, "_terminate", terminate)
    monkeypatch.setattr(sb, "_kill", kill)
    lines = stop_browser(home, wait_seconds=0.01, sleep=lambda s: None)
    assert terminated == [500, 600]
    assert killed == [600]
    assert "pid 500, 600" in lines[0]
    assert not home.pid_file.exists()
    # A supervising run is told to stay down; killing its child alone would
    # just make it start another browser (every installed unit supervises).
    assert sb.stop_requested(home)


def test_stop_browser_reports_nothing_running(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    monkeypatch.setattr(sb, "stray_chrome_pids", lambda *a, **k: [])
    assert stop_browser(home)[0].startswith("No server browser")


# --- service identity (E18, E19) ------------------------------------------------------------------


def test_service_spec_is_per_port():
    a = service_spec(9222)
    b = service_spec(9223)
    assert a.systemd_unit == "nymeria-browser-9222.service"
    assert b.systemd_unit == "nymeria-browser-9223.service"
    assert a.launchd_label == "com.nymeria.browser.9222"
    assert a.windows_task_name and "9222" in a.windows_task_name
    assert a.syslog_identifier == "nymeria-browser"
    assert a.install_hint == "nymeria browser service install"


def test_service_exec_argv_runs_the_browser_supervised(tmp_path, monkeypatch):
    monkeypatch.setattr(sb.sys, "frozen", True, raising=False)
    argv = service_exec_argv(tmp_path)
    assert argv[1:] == ["browser", "run", "--root", str(tmp_path), "--supervise"]


# --- status ------------------------------------------------------------------------------------------


def test_status_reports_unconfigured_rig(tmp_path):
    report = status(RigHome(tmp_path / "rig"), http_json=lambda *a, **k: {}, probe_backend=False)
    assert not report.installed and not report.configured
    assert any("not installed" in p for p in report.problems)


def real_roster_lines(client_id: str, *, connected: bool, label: str | None, version="0.29.0"):
    """The exact strings `/browser list` returns, from the function that makes them.

    `data["browsers"]` is a list of DISPLAY STRINGS, not dicts. Deriving the
    fixture from `browser_targets.roster_lines` is the point: a hand-written
    dict fixture is what let the status parser ship reading a shape the
    backend never produces.
    """
    from nymeria.core import browser_targets, chrome_subscribers

    chrome_subscribers.reset_for_tests()
    try:
        chrome_subscribers.add_chrome_subscriber(
            user_id="u",
            subscriber_id="sub-1",
            client_id=client_id,
            version=version,
            kind="server",
        )
        chrome_subscribers.add_chrome_subscriber(
            user_id="u",
            subscriber_id="sub-2",
            client_id="nymeria-browser-99999999-other",
            version="0.29.0",
            kind="desktop",
        )
        if not connected:
            chrome_subscribers.remove_chrome_subscriber("sub-1")
        original = browser_targets.browser_labels
        browser_targets.browser_labels = lambda user_id: ({client_id: label} if label else {})
        try:
            return browser_targets.roster_lines("u")
        finally:
            browser_targets.browser_labels = original
    finally:
        chrome_subscribers.reset_for_tests()


def _status_probe(home, rows, *, worker_present=True, port=9400):
    def http_json(url, token=None, method="GET", body=None, timeout=4.0):
        if url.endswith("/json/version"):
            return {"Browser": "Chrome/152.0.7977.64"}
        if url.endswith(f":{port}/json"):
            targets = [{"type": "page", "url": "chrome://newtab/"}]
            if worker_present:
                targets.append(
                    {
                        "type": "service_worker",
                        "url": f"chrome-extension://{EXTENSION_ID}/background.js",
                    }
                )
            return targets
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/me"):
            assert token == "t"
            return {"id": "default", "email": "me@example.com"}
        if url.endswith("/commands/execute"):
            assert method == "POST" and body is not None and body["command"] == "/browser list"
            return {"markdown": "", "data": {"browsers": rows}}
        raise AssertionError(url)

    return status(home, http_json=http_json)


def test_status_reads_debug_port_and_backend_roster(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    config = RigConfig.load(home)
    assert config is not None
    monkeypatch.setattr(sb, "running_pid", lambda h, run=None: 4321)
    rows = real_roster_lines(config.client_id, connected=True, label="server browser")
    assert any(line.startswith("server browser (") and ": connected" in line for line in rows)

    report = _status_probe(home, rows)
    assert report.running and report.chrome_version == "Chrome/152.0.7977.64"
    assert report.extension_worker_present is True
    assert report.token_valid and report.identity == "me@example.com"
    assert report.connected_per_backend is True
    assert report.healthy and not report.problems
    assert any("this browser is connected" in line for line in report.lines)


def test_status_reads_the_roster_for_every_row_shape_the_backend_makes(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    config = RigConfig.load(home)
    assert config is not None
    monkeypatch.setattr(sb, "running_pid", lambda h, run=None: 4321)

    for label in ("server browser", None):
        connected = _status_probe(
            home, real_roster_lines(config.client_id, connected=True, label=label)
        )
        assert connected.connected_per_backend is True, label
        dropped = _status_probe(
            home, real_roster_lines(config.client_id, connected=False, label=label)
        )
        assert dropped.connected_per_backend is False, label
        assert any("NOT connected" in p for p in dropped.problems)
    # A roster that does not name this browser at all stays unknown.
    absent = _status_probe(
        home, real_roster_lines("nymeria-browser-deadbeef-nope", connected=True, label="other")
    )
    assert absent.connected_per_backend is None
    assert any("not in the roster yet" in line for line in absent.lines)


def test_connected_in_roster_reads_the_short_id_not_the_last_eight_characters():
    client_id = "nymeria-browser-a1b2c3d4-5555-6666-7777-888888888888"
    row = "server browser (a1b2c3d4, server): connected, v0.29.0"
    assert sb.connected_in_roster([row], client_id) is True
    assert sb.connected_in_roster(["a1b2c3d4 (server): disconnected 12s ago"], client_id) is False
    assert sb.connected_in_roster([], client_id) is None
    # The old parser looked for the LAST eight characters and for dicts.
    assert sb.connected_in_roster(["88888888 (server): connected"], client_id) is None
    assert sb.connected_in_roster([{"client_id": client_id, "connected": True}], client_id) is None


def test_status_reports_an_idle_stopped_worker_without_calling_the_rig_unhealthy(tmp_path, monkeypatch):
    # An MV3 worker that has idle-stopped is absent and perfectly healthy; the
    # backend's view is the truth. The field says what was measured.
    home = _configured_home(tmp_path)
    config = RigConfig.load(home)
    assert config is not None
    monkeypatch.setattr(sb, "running_pid", lambda h, run=None: 4321)
    report = _status_probe(
        home,
        real_roster_lines(config.client_id, connected=True, label="server browser"),
        worker_present=False,
    )
    assert report.extension_worker_present is False
    assert report.healthy


def test_status_reports_an_unreadable_rig_json_instead_of_raising(tmp_path):
    home = _configured_home(tmp_path)
    home.rig_json.write_text("{oops")
    report = status(home, http_json=lambda *a, **k: {}, probe_backend=False)
    assert not report.healthy
    assert any("cannot be read" in p for p in report.problems)


def test_status_flags_rejected_token(tmp_path, monkeypatch):
    home = _configured_home(tmp_path)
    monkeypatch.setattr(sb, "running_pid", lambda h, run=None: None)

    def http_json(url, token=None, method="GET", body=None, timeout=4.0):
        if url.endswith("/json/version"):
            raise OSError("refused")
        if url.endswith("/health"):
            return {}
        if url.endswith("/me"):
            raise OSError("HTTP 401")
        raise AssertionError(url)

    report = status(home, http_json=http_json)
    assert report.running is False and report.token_valid is False
    assert any("token was rejected" in p for p in report.problems)
    assert any("Not running" in p for p in report.problems)
    assert not report.healthy


# --- provision (the wizard's call) ----------------------------------------------------------------


def test_provision_never_raises_and_hands_back_manual_commands(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    def failing_fetch(url):
        raise OSError("offline")

    report = provision(
        root,
        base_url="http://localhost:8000",
        token="t",
        fetch=failing_fetch,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert report.ok is False
    assert any("Could not install Chrome for Testing" in w for w in report.warnings)
    assert any(cmd.startswith("nymeria browser install") for cmd in report.manual_commands)


@pytest.mark.parametrize("victim", ["stage_extension", "bake_config"])
def test_provision_survives_failures_that_are_not_server_browser_errors(tmp_path, monkeypatch, victim):
    # "Never raises" has to mean it: the paths under provision rmtree,
    # copytree, mkdir and rename, and a traceback out of `nymeria init` is
    # exactly what this promise exists to prevent.
    root = tmp_path / "root"
    root.mkdir()
    build = make_build_dir(tmp_path)

    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cft_zip_bytes())

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sb, victim, boom)
    report = provision(
        root,
        base_url="http://localhost:8000",
        token="tok",
        source=build,
        fetch=lambda url: _cft_payload("https://cft/linux64.zip"),
        download=download,
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert report.ok is False
    assert any("No space left on device" in w for w in report.warnings)
    assert any("nymeria browser configure" in cmd for cmd in report.manual_commands)


def test_provision_survives_a_service_manager_that_raises_anything(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    build = make_build_dir(tmp_path)

    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cft_zip_bytes())

    class ExplodingManager:
        def install(self, *, exec_argv, root):
            raise UnicodeEncodeError("ascii", "C:/Users/Jos\u00e9", 12, 13, "ordinal not in range")

    monkeypatch.setattr(sb, "browser_service_manager", lambda home, runner=None: ExplodingManager())
    monkeypatch.setattr(sb.sys, "frozen", True, raising=False)
    report = provision(
        root,
        base_url="http://localhost:8000",
        token="tok",
        source=build,
        fetch=lambda url: _cft_payload("https://cft/linux64.zip"),
        download=download,
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert report.ok and not report.service_installed
    assert any("Could not install the browser service" in w for w in report.warnings)
    assert any("service install" in cmd for cmd in report.manual_commands)


def test_provision_installs_configures_and_installs_service(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    build = make_build_dir(tmp_path)
    runner = FakeRunner()

    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cft_zip_bytes())

    class FakeManager:
        name = "fake"

        def __init__(self):
            self.installed_with = None

        def install(self, *, exec_argv, root):
            self.installed_with = (list(exec_argv), root)
            from nymeria.service_install import InstallReport

            return InstallReport(artifact=root / "unit", lines=("installed",), notes=("note",))

    manager = FakeManager()
    monkeypatch.setattr(sb, "browser_service_manager", lambda home, runner=None: manager)
    monkeypatch.setattr(sb.sys, "frozen", True, raising=False)
    report = provision(
        root,
        base_url="http://localhost:8000",
        token="tok",
        client_id="nymeria-browser-fixed",
        label="server browser",
        source=build,
        fetch=lambda url: _cft_payload("https://cft/linux64.zip"),
        download=download,
        run=runner,
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert report.ok and report.service_installed
    assert report.config is not None and report.config.client_id == "nymeria-browser-fixed"
    assert manager.installed_with is not None
    assert manager.installed_with[0][1:] == ["browser", "run", "--root", str(root), "--supervise"]
    home = resolve_rig_home(root)
    assert json.loads(home.baked_config.read_text())["clientId"] == "nymeria-browser-fixed"
    assert "installed" in report.lines and "note" in report.lines


def test_provision_without_service_prints_run_command(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    build = make_build_dir(tmp_path)

    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cft_zip_bytes())

    report = provision(
        root,
        base_url="http://localhost:8000",
        token="tok",
        source=build,
        install_service=False,
        fetch=lambda url: _cft_payload("https://cft/linux64.zip"),
        download=download,
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )
    assert report.ok and not report.service_installed
    assert report.manual_commands == [f"nymeria browser run --root {root}"]


# --- CLI wiring ---------------------------------------------------------------------------------------------


def test_run_py_registers_browser_subcommand():
    import run as run_module

    parser = run_module.build_parser()
    args = parser.parse_args(["browser", "configure", "--root", "/r", "--sandbox", "off", "--token-file", "/t"])
    assert args.command == "browser" and args.action == "configure"
    assert args.root == "/r" and args.sandbox == "off" and args.token_file == "/t"
    service_args = parser.parse_args(["browser", "service", "install"])
    assert service_args.service_action == "install"
    assert run_module.COMMANDS["browser"].exits is True
    assert "browser" not in run_module._FULL_VALIDATION_COMMANDS


def test_browser_cli_configure_reads_token_file_and_defaults_base_url(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.env").write_text("API_PORT=8010\n")
    token_file = tmp_path / "token.txt"
    token_file.write_text("nym_from_file\n")
    seen: dict = {}

    def fake_configure(home, **kwargs):
        seen.update(kwargs)
        seen["home"] = home.path
        return RigConfig(client_id="nymeria-browser-x", base_url=kwargs["base_url"])

    monkeypatch.setattr(sb, "configure", fake_configure)
    import argparse

    args = argparse.Namespace(
        root=str(root),
        home=None,
        action="configure",
        base_url=None,
        token=None,
        token_file=str(token_file),
        token_stdin=False,
        client_id=None,
        label=None,
        source=None,
        debug_port=None,
        sandbox="auto",
        adopt_home=None,
    )
    assert sb.browser_cli(args) == 0
    assert seen["token"] == "nym_from_file"
    assert seen["base_url"] == "http://localhost:8010"
    assert seen["home"] == (root / "data" / "server-browser").resolve()


def test_browser_cli_reports_errors_with_hints(tmp_path, capsys):
    import argparse

    root = tmp_path / "root"
    root.mkdir()
    args = argparse.Namespace(root=str(root), home=None, action="run", fresh_profile=False, supervise=False)
    code = sb.browser_cli(args)
    out = capsys.readouterr().out
    assert code == 1
    assert "no server browser configured" in out and "nymeria browser configure" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pid probe")
def test_pid_alive_true_for_self_false_for_dead():
    assert sb.pid_alive(os.getpid()) is True
    assert sb.pid_alive(0) is False
    # A pid that WAS a process and is not one any more: reaped, so the number
    # is free and nothing on this box can be holding it a microsecond later.
    child = subprocess.Popen([sys.executable, "-c", "pass"], env=os.environ.copy())
    child.wait()
    assert sb.pid_alive(child.pid) is False


def test_child_env_carries_no_nymeria_secret(monkeypatch):
    # The one direct assertion behind every `env=child_env()` call site: the
    # FakeRunner's "env is not None" is satisfied by a full os.environ copy.
    for name in (
        "NYMERIA_SECRETS_KEY",
        "NYMERIA_SERVICE_TOKEN",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, f"secret-{name}")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    env = sb.child_env()
    assert not [k for k in env if k.startswith("NYMERIA_")]
    for name in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABASE_URL"):
        assert name not in env
    assert not [v for v in env.values() if v.startswith("secret-")]
    # A browser behind a corporate proxy still works.
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["PATH"] == os.environ["PATH"]


def test_token_is_read_from_stdin_only_when_asked_for(monkeypatch):
    import argparse

    monkeypatch.setattr(sb.sys, "stdin", io.StringIO("unrelated pipeline input\n"))
    args = argparse.Namespace(token=None, token_file=None, token_stdin=False)
    with pytest.raises(ServerBrowserError, match="needs an account token"):
        sb._token_from_args(args)
    monkeypatch.setattr(sb.sys, "stdin", io.StringIO("nym_piped\n"))
    assert sb._token_from_args(argparse.Namespace(token=None, token_file=None, token_stdin=True)) == "nym_piped"
    monkeypatch.setattr(sb.sys, "stdin", io.StringIO("   \n"))
    with pytest.raises(ServerBrowserError, match="stdin held no token"):
        sb._token_from_args(argparse.Namespace(token=None, token_file=None, token_stdin=True))


def test_configure_records_a_non_default_rig_home_for_the_wizard(tmp_path, monkeypatch):
    # Without this, `configure --home /srv/rig` then `nymeria init` provisions
    # a SECOND rig at the default home and revokes the first one's token.
    import argparse

    root = tmp_path / "root"
    root.mkdir()
    custom = tmp_path / "srv" / "rig"
    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)
    monkeypatch.setattr(sb, "configure", lambda home, **kwargs: RigConfig(client_id="nymeria-browser-x", base_url="http://x"))
    token_file = tmp_path / "tok"
    token_file.write_text("t\n")
    args = argparse.Namespace(
        root=str(root), home=str(custom), action="configure", base_url=None, token=None,
        token_file=str(token_file), token_stdin=False, client_id=None, label=None,
        source=None, debug_port=None, sandbox="on", adopt_home=None,
    )
    assert sb.browser_cli(args) == 0
    assert sb.resolve_rig_home(root).path == custom.resolve()
    # A bare `configure` now targets that same rig rather than a second one.
    args.home = None
    assert sb.browser_cli(args) == 0
    assert sb.resolve_rig_home(root).path == custom.resolve()
    # Configuring the default home again clears the pointer.
    args.home = str(root / "data" / "server-browser")
    assert sb.browser_cli(args) == 0
    assert not sb.rig_home_pointer(root).exists()
    assert sb.resolve_rig_home(root).path == (root / "data" / "server-browser").resolve()


def test_rig_home_pointer_ranks_below_the_env_files(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)
    sb.rig_home_pointer(root).write_text(f"{tmp_path / 'pointed'}\n")
    assert sb.resolve_rig_home(root).path == (tmp_path / "pointed").resolve()
    (root / "config.env").write_text(f'{sb.HOME_ENV_KEY}="{tmp_path / "from-env"}"\n')
    assert sb.resolve_rig_home(root).path == (tmp_path / "from-env").resolve()


def test_client_id_and_label_stay_inside_what_the_extension_accepts():
    # A value the backend bakes and the extension refuses is worse than a
    # refusal here: the rig silently keeps its previous identity or label.
    with pytest.raises(ServerBrowserError, match="letters, digits"):
        sb.validate_client_id("nymeria-browser-abc\r\nX-Injected: 1")
    with pytest.raises(ServerBrowserError, match="letters, digits"):
        sb.validate_client_id("nymeria-browser-a b")
    with pytest.raises(ServerBrowserError, match="at most 128 characters"):
        sb.validate_client_id("nymeria-browser-" + "a" * 200)
    assert sb.validate_client_id("  nymeria-browser-a1b2.c3_d4-5  ") == "nymeria-browser-a1b2.c3_d4-5"

    assert sb.clean_label("bell\x07 and\u200d joiner") == "bell and joiner"
    assert sb.clean_label("rtl\u202eoverride") == "rtloverride"
    assert sb.clean_label("two\u2028lines") == "two lines"
    assert sb.clean_label("\x00\x01") == DEFAULT_LABEL
    assert len(sb.clean_label("x" * 200)) == 60


def test_configure_refuses_a_base_url_that_is_not_absolute_http(tmp_path):
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    for bad in ("localhost:8000", "ftp://box:8000", "http:///nohost", "/just/a/path"):
        with pytest.raises(ServerBrowserError, match="absolute http"):
            configure(
                home,
                base_url=bad,
                token="t",
                source=build,
                sandbox="on",
                run=(runner := FakeRunner()),
                popen=runner.popen,
                log=lambda _: None,
                platform_key="linux64",
            )
    assert sb.validate_base_url("https://nymeria.example.com/") == "https://nymeria.example.com"


def test_mac_extract_uses_a_symlink_preserving_tool_and_names_a_missing_one(tmp_path, monkeypatch):
    # zipfile turns the .app bundle's framework symlinks into plain files, so
    # the extracted browser cannot resolve its own framework.
    zip_path = tmp_path / "cft.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("chrome-mac-arm64/marker", "x")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        assert kwargs.get("env") is not None
        calls.append(list(argv))
        return completed(argv, 0)

    monkeypatch.setattr(sb.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "ditto" else None)
    sb._extract_zip(zip_path, tmp_path / "mac", platform_key="mac-arm64", run=runner)
    assert calls and calls[0][0] == "ditto" and calls[0][1:3] == ["-x", "-k"]
    assert not (tmp_path / "mac" / "chrome-mac-arm64" / "marker").exists()

    # Linux and Windows keep the zipfile path and never shell out.
    calls.clear()
    sb._extract_zip(zip_path, tmp_path / "linux", platform_key="linux64", run=runner)
    assert calls == []
    assert (tmp_path / "linux" / "chrome-mac-arm64" / "marker").is_file()

    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    with pytest.raises(ServerBrowserError, match="ditto, then unzip"):
        sb._extract_zip(zip_path, tmp_path / "mac2", platform_key="mac-x64", run=runner)


def test_install_leaves_no_half_extracted_browser_when_the_mac_tool_is_missing(tmp_path, monkeypatch):
    home = RigHome(tmp_path / "rig")

    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cft_zip_bytes())

    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    with pytest.raises(ServerBrowserError, match="symlink-preserving"):
        install(
            home,
            fetch=lambda url: json.dumps(
                {
                    "channels": {
                        "Stable": {
                            "version": "152.0.7977.64",
                            "downloads": {"chrome": [{"platform": "mac-arm64", "url": "https://x.zip"}]},
                        }
                    }
                }
            ).encode(),
            download=download,
            run=FakeRunner(),
            log=lambda _: None,
            platform_key="mac-arm64",
        )
    # Nothing at all is left under cft/: no version dir, and no half-extracted
    # staging dir for `chrome_binary` (or the next install) to trip over.
    assert list(home.cft_dir.glob("*")) == []


def test_resolve_rig_home_can_answer_for_the_root_alone(tmp_path, monkeypatch):
    """`process_env=False` is setup's view: the launch shell's export names
    ANOTHER install's rig on a two-install host, so it must lose to the root's
    own env files and, failing those, to the root's default home."""
    import nymeria.server_browser as sb

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv(sb.HOME_ENV_KEY, str(tmp_path / "elsewhere"))

    assert sb.resolve_rig_home(root, process_env=False).path == (root / "data" / "server-browser").resolve()
    # The CLI-shaped default still lets the shell win.
    assert sb.resolve_rig_home(root).path == (tmp_path / "elsewhere").resolve()

    (root / "config.env").write_text(f"{sb.HOME_ENV_KEY}={root / 'rig'}\n")
    assert sb.resolve_rig_home(root, process_env=False).path == (root / "rig").resolve()


def test_install_resolves_its_http_helpers_at_call_time(tmp_path, monkeypatch):
    """The suite keeps the launcher offline by patching `_fetch_bytes` and
    `_download_file`; that only holds if `install` looks them up when called
    rather than binding them as defaults at import."""
    import nymeria.server_browser as sb

    home = RigHome(tmp_path / "rig")
    seen: list[str] = []

    def fake_download(url, dest, *, log=print):
        seen.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cft_zip_bytes())

    monkeypatch.setattr(sb, "_fetch_bytes", lambda url, timeout=30.0: _cft_payload("https://cft/linux64.zip"))
    monkeypatch.setattr(sb, "_download_file", fake_download)

    result = install(home, run=FakeRunner(), log=lambda _: None, platform_key="linux64")

    assert seen == ["https://cft/linux64.zip"]
    assert result.binary.is_file()


def test_the_suite_keeps_the_launcher_off_the_network(tmp_path):
    """Regression pin for the conftest guard: an unfaked `install` must fail
    fast with a message naming the fix, never read the Chrome for Testing feed
    (the wizard hook once downloaded a real browser from a generic test)."""
    home = RigHome(tmp_path / "rig")
    with pytest.raises(ServerBrowserError) as excinfo:
        install(home, run=FakeRunner(), log=lambda _: None, platform_key="linux64")
    assert "offline" in str(excinfo.value)
    assert not home.cft_dir.exists()


def test_browser_cli_with_an_explicit_root_ignores_the_launch_environment(tmp_path, monkeypatch):
    """`nymeria browser configure --root <B>` is the wizard's own printed Docker
    remediation, and run.py has merged the LAUNCH root's env into os.environ by
    the time it runs. Env-first here re-baked install A's rig with B's token;
    an explicit --root must answer for B alone. Without --root the launch root
    is the target and a shell export stays a deliberate override."""
    import argparse

    root = tmp_path / "root"
    root.mkdir()
    (root / "config.env").write_text("API_PORT=8010\n")
    other_rig = (tmp_path / "other-install" / "rig").resolve()
    monkeypatch.setenv(sb.HOME_ENV_KEY, str(other_rig))
    seen: list = []
    monkeypatch.setattr(sb, "install", lambda home, **kw: seen.append(home.path))

    def args(root_arg):
        return argparse.Namespace(root=root_arg, home=None, action="install")

    assert sb.browser_cli(args(str(root))) == 0
    assert seen == [(root / "data" / "server-browser").resolve()]

    monkeypatch.setattr("nymeria._runtime_paths.configure_project_root", lambda start=None: root)
    assert sb.browser_cli(args(None)) == 0
    assert seen[-1] == other_rig


def test_probe_sandbox_outlives_chrome_children_still_writing_its_profile(tmp_path, monkeypatch):
    """The stop waits for the browser process only; its helpers can keep touching
    the throwaway profile for a moment, so the first rmtree fails ENOTEMPTY. Seen adopting a live rig: configure died in the temp dir's
    cleanup after the probe already had its answer. The answer must come back,
    the removal is retried, and the directory ends up gone."""
    import errno

    real_rmtree = sb.shutil.rmtree
    attempts: list[str] = []

    def flaky_rmtree(path, *args, **kwargs):
        attempts.append(str(path))
        if len(attempts) <= 2 and not kwargs.get("ignore_errors"):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", "Default")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(sb.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(sb.time, "sleep", lambda _s: None)
    runner = FakeRunner(sandbox_ok=True)
    profiles: list[str] = []

    def popen(argv, **kwargs):
        profiles.append(next(a for a in argv if a.startswith("--user-data-dir=")).split("=", 1)[1])
        return runner.popen(argv, **kwargs)

    decision = probe_sandbox(tmp_path / "chrome", popen=popen, log=lambda _: None)

    assert decision.no_sandbox is False
    assert len(attempts) == 3 and all(a == profiles[0] for a in attempts)
    assert not Path(profiles[0]).exists()


def test_probe_gives_up_on_a_browser_that_never_opens_devtools_and_still_stops_it(tmp_path):
    """The deadline path: a Chrome that neither dies nor announces DevTools
    (the `--dump-dom` hang, seen live on CfT 152 and 153) is reported as a
    failure with the deadline in the reason, and its process tree is stopped
    rather than left running behind the wizard."""
    spawned: list[FakeProc] = []

    def popen(argv, **kwargs):
        proc = FakeProc([])
        spawned.append(proc)
        return proc

    result = sb._probe_once([str(tmp_path / "chrome")], popen=popen, timeout=0.2)

    assert result.ok is False and "no DevTools endpoint after 0s" in result.detail
    assert spawned and spawned[0].stopped


def test_probe_stops_the_browser_it_started_even_on_success(tmp_path):
    """Readiness is the answer; the probe Chrome must not outlive the question."""
    runner = FakeRunner(sandbox_ok=True)
    spawned: list = []

    def popen(argv, **kwargs):
        proc = runner.popen(argv, **kwargs)
        spawned.append(proc)
        return proc

    assert probe_sandbox(tmp_path / "chrome", popen=popen, log=lambda _: None).no_sandbox is False
    assert [p.stopped for p in spawned] == [True]


def test_configure_clears_the_profiles_cached_worker_script(tmp_path):
    """Chrome runs the service-worker SCRIPT it cached in the profile, keyed by
    the extension's (key-pinned) origin, so a profile that last ran an older
    build keeps executing it after a restart while reporting the new manifest
    version. Measured live on an adopted v0.28.0 profile: two starts ran the
    old worker and never adopted the bake. Every configure (which has already
    stopped the rig) drops that cache; the sessions beside it stay."""
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    cache = home.profile_dir / "Default" / "Service Worker" / "ScriptCache"
    cache.mkdir(parents=True)
    (cache / "index").write_bytes(b"stale 0.28.0 worker")
    (home.profile_dir / "Default" / "Cookies").write_text("session-bytes")
    lines: list[str] = []

    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=build,
        sandbox="on",
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lines.append,
        platform_key="linux64",
    )

    assert not (home.profile_dir / "Default" / "Service Worker").exists()
    assert (home.profile_dir / "Default" / "Cookies").read_text() == "session-bytes"
    assert any("cached service-worker scripts" in line for line in lines)


def test_adopting_a_profile_does_not_carry_its_cached_worker_script(tmp_path):
    """The adopt path is where this bit: the copied profile brought the old
    rig's worker cache along. The copy keeps the sessions and loses the cache;
    the original is left exactly as it was."""
    home = make_home(tmp_path)
    build = make_build_dir(tmp_path)
    old = tmp_path / "old-rig"
    old_cache = old / "profile" / "Default" / "Service Worker" / "ScriptCache"
    old_cache.mkdir(parents=True)
    (old_cache / "index").write_bytes(b"stale")
    (old / "profile" / "Default" / "Cookies").write_text("session-bytes")

    configure(
        home,
        base_url="http://localhost:8000",
        token="t",
        source=build,
        sandbox="on",
        adopt_home=old,
        run=(runner := FakeRunner()),
        popen=runner.popen,
        log=lambda _: None,
        platform_key="linux64",
    )

    assert not (home.profile_dir / "Default" / "Service Worker").exists()
    assert (home.profile_dir / "Default" / "Cookies").read_text() == "session-bytes"
    assert (old_cache / "index").exists()


def test_probe_failure_reason_is_the_fatal_line_not_the_register_dump():
    """The reason lands in rig.json and `status`; a real sandbox failure puts the
    one meaningful line at the top of stderr and a crash dump (frames, register
    rows, `[end of stack trace]`) at the bottom, which is what the old
    last-three-lines rule persisted."""
    stderr = [
        "[4011785:4011790:0909/153758.900000:WARNING:sandbox/policy/linux/sandbox_linux.cc:393] "
        "InitializeSandbox() called with multiple threads in process gpu-process.",
        "[4011785:4011785:0909/153758.910897:FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] "
        "No usable sandbox! If you are running on Ubuntu 23.10+ or another Linux distro that has disabled "
        "unprivileged user namespaces with AppArmor, see https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md.",
        "[0909/153758.919103:ERROR:third_party/crashpad/crashpad/util/file/file_io_posix.cc:145] open /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq: No such file or directory (2)",
        "[0909/153758.919169:ERROR:third_party/crashpad/crashpad/util/file/file_io_posix.cc:145] open /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq: No such file or directory (2)",
        "[4011785:4011785:0909/153758.920001:ERROR:dbus/object_proxy.cc:572] Failed to call method: org.freedesktop.DBus.Properties.GetAll",
        "[4011785:4011785:0909/153758.920100:ERROR:dbus/bus.cc:405] Failed to connect to the bus: Could not parse server address",
        "Received signal 6",
        "#0 0x5c8da75de6b3 (/opt/rig/cft/153.0.8010.36/chrome-linux64/chrome+0x6a076b2)",
        "#1 0x5c8dac2fa2e4 (/opt/rig/cft/153.0.8010.36/chrome-linux64/chrome+0xb7232e3)",
        "  r8: 000029600013c269  r9: 0000000000000001 r10: 0000000000000008 r11: 0000000000000246",
        "  ip: 0000716a5be9ec0c efl: 0000000000000246 cgf: 002b000000000033 erf: 0000000000000000",
        "[end of stack trace]",
    ]
    reason = sb._probe_failure_summary(stderr)
    assert reason.startswith("No usable sandbox! If you are running on Ubuntu 23.10+")
    assert "efl:" not in reason and "[4011785" not in reason

    # No FATAL: an ERROR whose message names the sandbox beats a WARNING that does.
    assert sb._probe_failure_summary([
        "[1:2:0909/000000.000000:WARNING:sandbox/policy/linux/sandbox_linux.cc:393] InitializeSandbox() called with multiple threads",
        "[1:1:0909/000000.000000:ERROR:content/browser/zygote_host/zygote_host_impl_linux.cc:200] Failed to move to new namespace: Operation not permitted",
        "#0 0xdeadbeef (chrome+0x1)",
    ]) == "Failed to move to new namespace: Operation not permitted"

    # No key line: the tail, minus crash-dump noise and prefixes.
    plain = sb._probe_failure_summary([
        "[1:1:0909/000000.000000:ERROR:gpu/ipc/service/gpu_init.cc:1] something odd",
        "#0 0xdeadbeef (chrome+0x1)",
        "[end of stack trace]",
    ])
    assert plain == "something odd"


def test_probe_stops_a_real_process_tree_once_devtools_is_up():
    """Real Popen, real process group: a child that announces DevTools and then
    sleeps must be reported ready AND be dead when the probe returns, not left
    sleeping behind the wizard. This is the branch the fakes cannot reach."""
    import sys

    script = "import sys, time; print('DevTools listening on ws://127.0.0.1:1/x', file=sys.stderr, flush=True); time.sleep(30)"
    spawned: list[subprocess.Popen] = []

    def popen(argv, **kwargs):
        proc = subprocess.Popen(argv, **kwargs)
        spawned.append(proc)
        return proc

    result = sb._probe_once([sys.executable, "-c", script], popen=popen, timeout=20)

    assert result.ok is True
    assert spawned and spawned[0].poll() is not None


def test_probe_reports_a_real_early_exit_with_its_fatal_line():
    import sys

    script = (
        "import sys; print('[1:1:0909/000000.000000:FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:129] "
        "No usable sandbox! probe', file=sys.stderr, flush=True); sys.exit(1)"
    )
    result = sb._probe_once([sys.executable, "-c", script], popen=subprocess.Popen, timeout=20)
    assert result.ok is False
    assert result.detail.startswith("No usable sandbox! probe")
