"""The server browser: a headless Chrome the agent can drive out of the box.

The `chrome_*` tools drive a browser through the `nymeria-browser` extension.
On a fresh install nobody has installed that extension anywhere, so the first
browsing request used to fail with "no extension connected". This module gives
every install a browser of its own: Chrome for Testing running headless beside
the backend, with the extension loaded and pre-connected to the local API as
the installing user. The setup wizard provisions it (`provision`); the
`nymeria browser` CLI manages it afterwards (`browser_cli`).

Why Chrome for Testing: branded Google Chrome dropped `--load-extension` in
v137 and `chrome-headless-shell` never supported extensions, so CfT is the
only build that runs an MV3 extension headless. Why a headful User-Agent:
the `HeadlessChrome/<v>` product token is a hard Google sign-in block
(measured 3/3 against CfT 152 on 2026-08-28), and
`--enable-automation` is an independent second trigger (`navigator.webdriver`),
so the UA is overridden with the headful string DERIVED from the binary's
major version and that flag is never passed.

How the extension gets connected with no popup: `configure` stages the
extension build with its optional host permissions rewritten as REQUIRED
(auto-granted at unpacked load, replacing every popup click) and bakes a
`config.json` into the package carrying the backend URL, an account token,
a pre-minted `clientId`, `kind: "server"` and a label. The extension's
service worker adopts it at startup and re-adopts whenever the file changes
(v0.29.0), so a re-configure (new port, rotated token) applies on the next
start without wiping the profile.

Layout under the rig home (default `<root>/data/server-browser`, or
`SERVER_BROWSER_HOME` in the root's env files):

    cft/<version>/...      the browser (one dir per installed version)
    ext/                   staged extension + baked config.json (0600)
    profile/               persistent --user-data-dir (0700): holds every
                           site session a human signed the browser into,
                           treat it like a private key
    rig.json               identity + choices (client_id, port, sandbox)
    chrome.pid             single-instance guard (taken with O_EXCL)
    stop.request           "do not restart" flag a supervising `run` honours,
                           so `stop` ends a service-installed rig instead of
                           handing it back to its own supervisor

The 0700/0600 modes above are asked for and then MEASURED: Windows cannot
express them and network filesystems drop them, so every hardening site says
what the filesystem actually kept rather than claiming the mode it wanted.

Sandbox: stock Ubuntu 24.04 restricts unprivileged user namespaces through
AppArmor and Chrome aborts at launch. Inference is unreliable (Ubuntu ships a
profile for `unshare` itself, so a userns probe passes while Chrome fails), so
`configure` measures: it launches Chrome once with the sandbox and, only if
that fails, once more with `--no-sandbox`, and persists the answer. The
opt-out is logged at every launch and surfaced by `nymeria doctor`.

Stdlib only, importable without Settings: the CLI must work on a box where
the backend's dependencies are half-installed, and the systemd unit runs
`nymeria browser run` on every boot.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .subprocess_env import NETWORK_RUNTIME_PASSTHROUGH, scrubbed_subprocess_env

# --- pinned extension release ---------------------------------------------------
#
# The backend and the extension share a wire contract (command types, the baked
# config fields, the client_kind announce), so an install pins the extension
# build the way it pins any dependency. Bump both constants together after
# publishing a release; the sha256 is the release asset's digest and an empty
# string means "not pinned yet" (fetch proceeds, digest printed, nothing to
# refuse against: only acceptable between the release and the pin commit).
EXTENSION_REPO = "ManningAskew7/nymeria-browser"
EXTENSION_RELEASE_TAG = "v0.29.0"
EXTENSION_RELEASE_SHA256 = "4c89e774d9854c04aff3a4aa44977cd03e009d8d0465a40b35e1542b23130dd4"
EXTENSION_RELEASES_URL = f"https://github.com/{EXTENSION_REPO}/releases"
# Pinned in the manifest's `key` since ext v0.25.0, so it is the same on every
# install; `status` selects the worker target by it (Chrome's own component
# extensions run their own `chrome-extension://` workers).
EXTENSION_ID = "hfjpeeimhfbkpidpeabpdppddahckhgp"

CFT_VERSIONS_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)

KIND = "server"
DEFAULT_LABEL = "server browser"
CLIENT_ID_PREFIX = "nymeria-browser-"
TOKEN_LABEL = "server-browser"
DEFAULT_DEBUG_PORT = 9222
WINDOW_SIZE = "1280,900"
# How long a supervised Chrome must stay up before its run counts as healthy
# and the restart budget goes back to full. Long enough that a crash loop
# (Chrome dies at startup, in seconds) never resets it, short enough that a
# browser which ran all afternoon is not still holding yesterday's tally.
HEALTHY_RUN_SECONDS = 300.0
# Env key finalize writes so the backend (refusals, doctor) knows this install
# has a server browser; the value is the rig home.
HOME_ENV_KEY = "SERVER_BROWSER_HOME"

# The Chrome for Testing platform keys whose payload is a macOS .app bundle
# (symlinks inside; see _extract_zip).
MAC_PLATFORM_KEYS = frozenset({"mac-arm64", "mac-x64"})

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)")
# Both caps and the id's character set mirror the extension's own validation
# of the baked config: a value the backend accepts and the extension refuses
# is worse than a refusal here, because the rig then keeps its old identity
# (or its old label) and nothing says why.
_LABEL_MAX_CHARS = 60
_CLIENT_ID_MAX_CHARS = 128
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


class ServerBrowserError(RuntimeError):
    """An operator-facing failure; the message says what to do."""

    def __init__(self, message: str, hints: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.hints: tuple[str, ...] = tuple(hints)


Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Fetcher = Callable[[str], bytes]
Downloader = Callable[[str, Path], None]


# --- platform -------------------------------------------------------------------


def cft_platform(system: str | None = None, machine: str | None = None) -> str:
    """The Chrome for Testing platform key for this host."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "linux64"
        raise ServerBrowserError(
            f"Chrome for Testing has no Linux build for {machine} (x86_64 only)."
        )
    if system == "darwin":
        return "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
    if system == "windows":
        if machine in {"amd64", "x86_64"}:
            return "win64"
        raise ServerBrowserError(
            f"Chrome for Testing has no Windows build for {machine} (64-bit only)."
        )
    raise ServerBrowserError(f"Chrome for Testing does not ship for {system}.")


def chrome_relative_binary(platform_key: str) -> Path:
    """Where the browser binary sits inside an extracted CfT zip."""
    if platform_key == "linux64":
        return Path("chrome-linux64") / "chrome"
    if platform_key == "win64":
        return Path("chrome-win64") / "chrome.exe"
    if platform_key in MAC_PLATFORM_KEYS:
        return (
            Path(f"chrome-{platform_key}")
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        )
    raise ServerBrowserError(f"unknown Chrome for Testing platform {platform_key}")


def resolve_stable_download(payload: dict, platform_key: str) -> tuple[str, str]:
    """(version, zip url) of the current Stable channel for ``platform_key``."""
    try:
        stable = payload["channels"]["Stable"]
        version = str(stable["version"])
        downloads = stable["downloads"]["chrome"]
    except (KeyError, TypeError) as exc:
        raise ServerBrowserError(
            "the Chrome for Testing version feed had an unexpected shape"
        ) from exc
    for entry in downloads:
        if entry.get("platform") == platform_key and entry.get("url"):
            return version, str(entry["url"])
    raise ServerBrowserError(
        f"the Chrome for Testing feed lists no {platform_key} download for {version}"
    )


def headful_user_agent(major: int, platform_key: str) -> str:
    """The UA a HEADFUL Chrome of this major would send on this platform.

    Reduced-UA format: everything below the major is zeroed, the macOS
    version is frozen at 10_15_7, and the product token is `Chrome`, never
    `HeadlessChrome`. Derived per launch from the installed binary so a CfT
    upgrade cannot leave a version-mismatched UA behind.
    """
    if platform_key == "linux64":
        system = "X11; Linux x86_64"
    elif platform_key == "win64":
        system = "Windows NT 10.0; Win64; x64"
    else:
        system = "Macintosh; Intel Mac OS X 10_15_7"
    return (
        f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def parse_chrome_version(text: str) -> str:
    """'Google Chrome for Testing 152.0.7977.64' -> '152.0.7977.64'."""
    match = _VERSION_RE.search(text or "")
    if not match:
        raise ServerBrowserError(f"could not read a Chrome version from {text!r}")
    return match.group(0)


# --- rig home -------------------------------------------------------------------


@dataclass(frozen=True)
class RigHome:
    """Paths of one server-browser install."""

    path: Path

    @property
    def cft_dir(self) -> Path:
        return self.path / "cft"

    @property
    def ext_dir(self) -> Path:
        return self.path / "ext"

    @property
    def profile_dir(self) -> Path:
        return self.path / "profile"

    @property
    def rig_json(self) -> Path:
        return self.path / "rig.json"

    @property
    def pid_file(self) -> Path:
        return self.path / "chrome.pid"

    @property
    def stop_request(self) -> Path:
        return self.path / "stop.request"

    @property
    def downloads_dir(self) -> Path:
        return self.path / "downloads"

    @property
    def baked_config(self) -> Path:
        return self.ext_dir / "config.json"

    def chrome_binary(self, platform_key: str | None = None) -> Path | None:
        """The newest installed browser binary, or None."""
        key = platform_key or cft_platform()
        rel = chrome_relative_binary(key)
        candidates: list[tuple[tuple[int, ...], Path]] = []
        if not self.cft_dir.is_dir():
            return None
        for version_dir in self.cft_dir.iterdir():
            binary = version_dir / rel
            if binary.is_file():
                candidates.append((_version_tuple(version_dir.name), binary))
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1][1]


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)) or (0,)


def write_atomic(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` through a temp file in the same directory.

    Neither of this module's two state files may ever be seen half-written: a
    truncated ``rig.json`` reads as "no server browser configured" (and used
    to make the next `configure` mint a NEW identity), and a truncated bake
    strands the extension with no backend to talk to. ``mode`` is applied to
    the temp file BEFORE the rename, so the 0600 bake is never briefly
    world-readable and keeps its mode across the swap.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None and os.name != "nt":
            try:
                # os.open's mode is masked by the umask; an explicit chmod is
                # not. Best effort: a filesystem that refuses chmod has
                # already given the file the mode it is going to have, and
                # `enforce_mode` at the call site is what reports that.
                os.chmod(tmp, mode)
            except OSError:
                pass  # best effort, see above; enforce_mode reports the kept mode
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # the original failure is the one worth raising
        raise


def enforce_mode(path: Path, mode: int) -> str:
    """Apply ``mode`` to ``path`` and report what the filesystem actually kept.

    Empty string means the mode is exactly what was asked for. Anything else
    is an operator-facing reason it is not: Windows cannot express POSIX modes
    at all, and network filesystems (a CIFS-mounted project dir is already a
    known shape here) accept the chmod and keep their mount-wide mode. The
    profile dir holds every signed-in site session, so a caller must never
    claim a mode it did not measure.
    """
    if os.name == "nt":
        return "Windows does not apply POSIX modes here; NTFS inheritance governs access"
    try:
        os.chmod(path, mode)
    except OSError as exc:
        return f"chmod {mode:04o} failed: {exc}"
    try:
        actual = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return f"the mode could not be read back: {exc}"
    if actual != mode:
        return f"the filesystem kept {actual:04o} instead of {mode:04o}"
    return ""


def _harden(path: Path, mode: int, what: str, log: Callable[[str], None]) -> bool:
    """chmod ``path``, and SAY SO when the mode did not stick. True when it did."""
    note = enforce_mode(path, mode)
    if note:
        log(
            f"WARNING: {what} ({path}) is not restricted to {mode:04o}: {note}. "
            "Anyone with an account on this host can read it."
        )
    return not note


def _read_env_value(root: Path, key: str) -> str | None:
    """``key`` from the root's env files, highest precedence last; tiny parser.

    Same discipline as ``service_install._read_env_port``: no dotenv import,
    so this module stays importable with zero project dependencies.
    """
    value: str | None = None
    for name in (".env", "config.env", ".env.docker"):
        try:
            lines = (root / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith(f"{key}="):
                continue
            raw = stripped.split("=", 1)[1].strip().strip("'\"")
            if raw:
                value = raw
    return value


def default_rig_home(root: Path) -> RigHome:
    return RigHome((root / "data" / "server-browser").resolve())


def rig_home_pointer(root: Path) -> Path:
    """Where the CLI records a rig home that is not ``root``'s default one."""
    return root / "data" / "server-browser-home"


def read_rig_home_pointer(root: Path) -> str | None:
    try:
        value = rig_home_pointer(root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_rig_home_pointer(root: Path, home: RigHome, *, log: Callable[[str], None]) -> None:
    """Record a non-default rig home so the next `nymeria init` finds THIS rig.

    `nymeria browser configure --home /srv/rig` used to leave no trace
    anywhere the wizard looks. A later `nymeria init` then resolved the
    DEFAULT home, found no rig.json, minted a SECOND identity, provisioned a
    second Chrome on the next free debug port with its own service unit, and
    revoked the first rig's token: two browsers, one of them dead. Finalize
    resolves the rig through :func:`resolve_rig_home`, so recording the home
    here is enough to make the hand-configured rig the one it reconfigures.
    """
    pointer = rig_home_pointer(root)
    if home.path == default_rig_home(root).path:
        pointer.unlink(missing_ok=True)
        return
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(pointer, f"{home.path}\n")
    except OSError as exc:
        log(
            f"WARNING: could not record this rig home in {pointer} ({exc}); "
            f"`nymeria init` will not see the rig at {home.path}. Set "
            f"{HOME_ENV_KEY}={home.path} in the install's env file instead."
        )
        return
    log(f"Recorded this rig home in {pointer} so `nymeria init` reconfigures it.")


def resolve_rig_home(
    root: Path, explicit: str | Path | None = None, *, process_env: bool = True
) -> RigHome:
    """The rig home for ``root``.

    Precedence: explicit > ``SERVER_BROWSER_HOME`` (process env, then the
    root's env files) > the pointer a `configure --home` left behind >
    ``<root>/data/server-browser``.

    ``process_env=False`` skips the process environment and answers for the
    ROOT alone. The CLI wants the env (a shell override should win); setup
    does not: `run.py` loads the LAUNCH root's dotenv into ``os.environ`` at
    import, so on a two-install host `nymeria init --root /other` would
    otherwise resolve, re-bake and re-token THIS install's running rig.
    """
    if explicit:
        return RigHome(Path(explicit).expanduser().resolve())
    from_env = (
        (os.environ.get(HOME_ENV_KEY) if process_env else None)
        or _read_env_value(root, HOME_ENV_KEY)
        or read_rig_home_pointer(root)
    )
    if from_env:
        return RigHome(Path(from_env).expanduser().resolve())
    return default_rig_home(root)


def resolve_api_port(root: Path) -> int:
    """API_PORT from the root's env files, else 8000 (same rule as service_install)."""
    raw = _read_env_value(root, "API_PORT")
    return int(raw) if raw and raw.isdigit() else 8000


@dataclass
class RigConfig:
    """The persisted identity and choices of one rig (``rig.json``)."""

    client_id: str
    base_url: str
    label: str = DEFAULT_LABEL
    kind: str = KIND
    debug_port: int = DEFAULT_DEBUG_PORT
    no_sandbox: bool = False
    sandbox_reason: str = ""
    extension_version: str = ""
    extension_source: str = ""
    cft_version: str = ""
    configured_at: str = ""

    def save(self, home: RigHome) -> None:
        home.path.mkdir(parents=True, exist_ok=True)
        write_atomic(home.rig_json, json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, home: RigHome) -> RigConfig | None:
        """The rig's identity, or None when nothing is configured here yet.

        A rig.json that EXISTS but cannot be read raises instead of reading as
        "unconfigured". Silently returning None there let `configure` take its
        no-previous-rig branch and mint a NEW client_id, which orphans the
        account's default-browser setting (it names the old id) while the
        docstring promised a stable identity. Losing the file is recoverable;
        silently re-identifying the browser is not.
        """
        try:
            raw_text = home.rig_json.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _unreadable_rig(home, str(exc)) from exc
        try:
            raw = json.loads(raw_text)
        except ValueError as exc:
            raise _unreadable_rig(home, f"it is not valid JSON ({exc})") from exc
        if not isinstance(raw, dict) or not raw.get("client_id") or not raw.get("base_url"):
            raise _unreadable_rig(home, "it names no client_id and base_url")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


def _unreadable_rig(home: RigHome, why: str) -> ServerBrowserError:
    return ServerBrowserError(
        f"{home.rig_json} exists but cannot be read: {why}. Refusing to act on a "
        "server browser whose identity is unreadable.",
        hints=(
            "Restore rig.json from a backup (or fix it by hand) to keep this browser's identity.",
            f"Or delete {home.rig_json} and re-run `nymeria browser configure`: that mints a "
            "NEW browser id, so re-point the account default with `/browser default <which>`.",
        ),
    )


def new_client_id() -> str:
    return f"{CLIENT_ID_PREFIX}{uuid.uuid4()}"


def validate_client_id(client_id: str) -> str:
    """The id this rig announces itself by, or a refusal naming the rule.

    Narrower than "starts with the prefix" because the extension validates
    the baked value field-wise and DROPS the whole bake when it fails: a
    client id the backend accepts and the extension rejects leaves the rig
    silently running on its previous identity. The id also travels as the
    `X-Nymeria-Client-Id` request header, where a CR or LF makes `fetch`
    reject the request forever, which the operator reads as "the backend is
    unreachable" rather than "the id is malformed".
    """
    cleaned = (client_id or "").strip()
    if not cleaned.startswith(CLIENT_ID_PREFIX) or len(cleaned) <= len(CLIENT_ID_PREFIX):
        raise ServerBrowserError(
            f"a browser client id must start with {CLIENT_ID_PREFIX!r} and name the browser"
        )
    if len(cleaned) > _CLIENT_ID_MAX_CHARS:
        raise ServerBrowserError(
            f"a browser client id may be at most {_CLIENT_ID_MAX_CHARS} characters "
            f"including the {CLIENT_ID_PREFIX!r} prefix (got {len(cleaned)})"
        )
    if not _CLIENT_ID_RE.fullmatch(cleaned):
        raise ServerBrowserError(
            "a browser client id may only hold letters, digits, dots, underscores "
            "and dashes (it travels as an HTTP header)"
        )
    return cleaned


def clean_label(label: str | None) -> str:
    """The roster label, reduced to what the extension will actually keep.

    Control and format characters (`unicodedata` categories Cc and Cf, plus
    the U+2028/U+2029 line separators) are stripped BEFORE whitespace is
    collapsed and before the truncation: the backend used to accept them
    happily while the extension dropped such a label whole, leaving the rig
    showing no label at all. The 60-character cap matches the extension's own.
    """
    kept: list[str] = []
    for ch in label or "":
        if ch.isspace() or ch in "\u2028\u2029":
            # Whitespace controls (tab, newline, U+2028) become a space and
            # collapse below, so a two-line label still reads as two words.
            kept.append(" ")
        elif unicodedata.category(ch) not in {"Cc", "Cf"}:
            kept.append(ch)
    text = " ".join("".join(kept).split())
    return text[:_LABEL_MAX_CHARS].strip() or DEFAULT_LABEL


def validate_base_url(base_url: str) -> str:
    """The backend URL, or a refusal naming the rule.

    Absolute http/https with a hostname. A schemeless value bakes fine today
    and produces a rig that can never connect: the extension refuses the whole
    bake, and `transform_manifest` reads the scheme to decide whether the
    origin needs a host permission, so it would silently grant nothing.
    """
    cleaned = (base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ServerBrowserError(
            f"the backend URL must be an absolute http:// or https:// URL with a host "
            f"(got {base_url!r})"
        )
    return cleaned


# --- HTTP helpers (injectable) -----------------------------------------------------


_HTTP_UA = "nymeria-server-browser/1"


def _fetch_bytes(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _download_file(url: str, dest: Path, *, log: Callable[[str], None] = print) -> None:
    """Stream ``url`` to ``dest`` with a coarse progress line."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -10
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = int(done * 100 / total)
                if pct >= last_pct + 10:
                    last_pct = pct
                    log(f"  {pct:3d}%  {done / (1 << 20):.0f} of {total / (1 << 20):.0f} MB")
    tmp.replace(dest)


def _extract_zip(
    zip_path: Path,
    dest: Path,
    *,
    platform_key: str = "",
    run: Runner = subprocess.run,
) -> None:
    """Extract ``zip_path`` into ``dest``, keeping what the archive holds.

    Two implementations, because Python cannot do the macOS one. ``zipfile``
    materialises a SYMLINK member as a regular file containing its target
    path, and Chrome for Testing's mac zips are `.app` bundles whose
    `Chromium Framework.framework` reaches its code through `Versions/Current`,
    `Helpers`, `Libraries` and `Resources` symlinks: extracted that way the app
    cannot resolve its own framework and dies at launch with a dyld error, on
    a platform where `_library_hints_for` has nothing to say. So the mac keys
    go through a symlink-preserving external tool (`ditto`, Apple's own, else
    `unzip`) and a missing tool is a named refusal rather than a half-extracted
    app. UNVERIFIED ON HARDWARE: no macOS host was available for this pass,
    the same standing as the Windows service path.

    Elsewhere zipfile plus mode restoration (zipfile alone drops the exec
    bits). Only the permission bits are restored (0o777): setuid, setgid and
    the sticky bit come from an archive off the network and are never needed.
    """
    if platform_key in MAC_PLATFORM_KEYS:
        _extract_zip_preserving_symlinks(zip_path, dest, run=run)
        return
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            target = Path(archive.extract(info, dest))
            mode = info.external_attr >> 16
            if mode and os.name != "nt":
                os.chmod(target, mode & 0o777)


def _extract_zip_preserving_symlinks(zip_path: Path, dest: Path, *, run: Runner) -> None:
    """Extract through `ditto` (else `unzip`): the only way to keep symlinks."""
    dest.mkdir(parents=True, exist_ok=True)
    attempts = (
        ["ditto", "-x", "-k", str(zip_path), str(dest)],
        ["unzip", "-q", "-o", str(zip_path), "-d", str(dest)],
    )
    for argv in attempts:
        if shutil.which(argv[0]) is None:
            continue
        try:
            result = run(argv, capture_output=True, text=True, timeout=600, env=child_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServerBrowserError(f"{argv[0]} could not extract {zip_path}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            raise ServerBrowserError(
                f"{argv[0]} could not extract {zip_path}: {detail[:300]}"
            )
        return
    raise ServerBrowserError(
        f"no symlink-preserving unzip tool was found to extract {zip_path} "
        "(looked for ditto, then unzip)",
        hints=(
            "ditto ships with macOS; on a system where it is missing, install unzip.",
            "Python's zipfile is not usable here: it turns the browser's framework "
            "symlinks into plain files and the app then cannot start.",
        ),
    )


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def child_env() -> dict[str, str]:
    """The environment a launched Chrome (or probe) may see.

    The allowlisted base plus proxy and CA settings: a browser behind a
    corporate proxy needs them, and none are Nymeria secrets.
    """
    return scrubbed_subprocess_env(NETWORK_RUNTIME_PASSTHROUGH)


# --- install ----------------------------------------------------------------------


@dataclass(frozen=True)
class InstallResult:
    version: str
    binary: Path
    already_installed: bool


# Shared-object names Chrome's binary links against on Linux, mapped to the
# Debian/Ubuntu package that ships them. Used only to turn an `ldd` "not found"
# list into an apt line; unknown names still print raw.
_LINUX_SONAME_PACKAGES = {
    "libnss3.so": "libnss3",
    "libnssutil3.so": "libnss3",
    "libsmime3.so": "libnss3",
    "libnspr4.so": "libnspr4",
    "libatk-1.0.so.0": "libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0",
    "libatspi.so.0": "libatspi2.0-0",
    "libcups.so.2": "libcups2",
    "libdrm.so.2": "libdrm2",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libxdamage1",
    "libXfixes.so.3": "libxfixes3",
    "libXrandr.so.2": "libxrandr2",
    "libgbm.so.1": "libgbm1",
    "libasound.so.2": "libasound2",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libcairo.so.2": "libcairo2",
    "libxshmfence.so.1": "libxshmfence1",
    "libX11.so.6": "libx11-6",
    "libXext.so.6": "libxext6",
    "libglib-2.0.so.0": "libglib2.0-0",
    "libgtk-3.so.0": "libgtk-3-0",
    "libdbus-1.so.3": "libdbus-1-3",
    "libexpat.so.1": "libexpat1",
}


def missing_shared_libraries(ldd_output: str) -> list[str]:
    """Sonames `ldd` reported as 'not found', in order, without duplicates."""
    found: list[str] = []
    for line in (ldd_output or "").splitlines():
        if "not found" not in line:
            continue
        name = line.strip().split(" ", 1)[0].split("=>", 1)[0].strip()
        if name and name not in found:
            found.append(name)
    return found


def missing_library_hint(sonames: Sequence[str]) -> list[str]:
    """Operator hints for a Chrome binary whose shared libraries are missing."""
    if not sonames:
        return [
            "The browser binary did not run. On a minimal Linux server install "
            "Chrome's shared libraries first (Debian/Ubuntu): sudo apt-get install -y "
            + " ".join(sorted(set(_LINUX_SONAME_PACKAGES.values()))),
        ]
    packages = sorted({_LINUX_SONAME_PACKAGES[s] for s in sonames if s in _LINUX_SONAME_PACKAGES})
    hints = [f"Missing shared libraries: {', '.join(sonames)}"]
    if packages:
        hints.append(
            "Debian/Ubuntu: sudo apt-get install -y " + " ".join(packages)
            + " (Ubuntu 24.04 names libasound2 as libasound2t64)"
        )
    unknown = [s for s in sonames if s not in _LINUX_SONAME_PACKAGES]
    if unknown:
        hints.append(
            "For the rest, `apt-file search <name>` (or your distro's equivalent) "
            "names the package."
        )
    return hints


def verify_binary(binary: Path, *, run: Runner = subprocess.run) -> str:
    """Run ``--version`` and return the version string, or raise with a fix."""
    try:
        result = run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServerBrowserError(
            f"the browser binary at {binary} did not run: {exc}",
            hints=_library_hints_for(binary, run),
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise ServerBrowserError(
            f"the browser binary at {binary} did not run: {detail[:300]}",
            hints=_library_hints_for(binary, run),
        )
    return parse_chrome_version(result.stdout or result.stderr)


def _library_hints_for(binary: Path, run: Runner) -> list[str]:
    if not sys.platform.startswith("linux"):
        return []
    try:
        ldd = run(
            ["ldd", str(binary)],
            capture_output=True,
            text=True,
            timeout=30,
            env=child_env(),
        )
        return missing_library_hint(missing_shared_libraries(ldd.stdout))
    except (OSError, subprocess.TimeoutExpired):
        return missing_library_hint([])


def install(
    home: RigHome,
    *,
    fetch: Fetcher | None = None,
    download: Downloader | None = None,
    run: Runner = subprocess.run,
    log: Callable[[str], None] = print,
    platform_key: str | None = None,
) -> InstallResult:
    """Fetch the current stable Chrome for Testing into ``home.cft_dir``.

    ``fetch`` and ``download`` resolve to the module's HTTP helpers at CALL
    time (not as bound defaults) so the test suite can keep the launcher
    offline by patching those two names; see `tests/conftest.py`.
    """
    key = platform_key or cft_platform()
    fetcher = fetch or _fetch_bytes
    downloader = download or (lambda url, dest: _download_file(url, dest, log=log))
    log("Resolving the current stable Chrome for Testing...")
    try:
        payload = json.loads(fetcher(CFT_VERSIONS_URL))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ServerBrowserError(
            f"could not read the Chrome for Testing version feed: {exc}"
        ) from exc
    version, url = resolve_stable_download(payload, key)
    version_dir = home.cft_dir / version
    binary = version_dir / chrome_relative_binary(key)
    if binary.is_file():
        log(f"Chrome for Testing {version} already installed.")
        return InstallResult(version=version, binary=binary, already_installed=True)
    home.cft_dir.mkdir(parents=True, exist_ok=True)
    zip_path = home.downloads_dir / f"cft-{version}-{key}.zip"
    log(f"Downloading Chrome for Testing {version} ({key})...")
    try:
        downloader(url, zip_path)
    except (urllib.error.URLError, OSError) as exc:
        raise ServerBrowserError(f"download failed: {exc}") from exc
    staging = home.cft_dir / f".{version}.extracting"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _extract_zip(zip_path, staging, platform_key=key, run=run)
    except ServerBrowserError:
        # Already an operator-facing message (the macOS path); never leave a
        # half-extracted browser behind for `chrome_binary` to find.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ServerBrowserError(f"could not extract {zip_path}: {exc}") from exc
    finally:
        zip_path.unlink(missing_ok=True)
    shutil.rmtree(version_dir, ignore_errors=True)
    staging.rename(version_dir)
    if not binary.is_file():
        raise ServerBrowserError(
            f"the archive did not contain {chrome_relative_binary(key)}"
        )
    installed_version = verify_binary(binary, run=run)
    log(f"Installed Chrome for Testing {installed_version} at {binary}")
    return InstallResult(version=installed_version, binary=binary, already_installed=False)


# --- extension staging ---------------------------------------------------------------


def extension_release_url(tag: str = EXTENSION_RELEASE_TAG) -> str:
    return f"https://github.com/{EXTENSION_REPO}/releases/download/{tag}/nymeria-browser-{tag}.zip"


def fetch_extension_release(
    home: RigHome,
    *,
    tag: str = EXTENSION_RELEASE_TAG,
    sha256: str = EXTENSION_RELEASE_SHA256,
    download: Downloader | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Download the pinned release zip and check its digest when pinned."""
    downloader = download or (lambda url, dest: _download_file(url, dest, log=log))
    url = extension_release_url(tag)
    dest = home.downloads_dir / f"nymeria-browser-{tag}.zip"
    if not dest.is_file():
        log(f"Downloading the browser extension {tag}...")
        try:
            downloader(url, dest)
        except (urllib.error.URLError, OSError) as exc:
            raise ServerBrowserError(
                f"could not download the extension release from {url}: {exc}",
                hints=(
                    f"Releases: {EXTENSION_RELEASES_URL}",
                    "Or pass --source <dir-or-zip> with a build you already have.",
                ),
            ) from exc
    actual = _sha256_of(dest)
    if sha256 and actual.lower() != sha256.lower():
        dest.unlink(missing_ok=True)
        raise ServerBrowserError(
            f"the extension release {tag} did not match its pinned digest "
            f"(expected {sha256}, got {actual}); refusing to stage it",
            hints=(f"Releases: {EXTENSION_RELEASES_URL}",),
        )
    if not sha256:
        log(f"  (release digest not pinned; sha256 {actual})")
    return dest


def _find_manifest_dir(root: Path) -> Path | None:
    if (root / "manifest.json").is_file():
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if (child / "manifest.json").is_file():
            return child
    return None


def stage_extension(source: Path, ext_dir: Path) -> str:
    """Copy a build dir or release zip into ``ext_dir``; return its manifest version."""
    source = Path(source)
    tmp_root: Path | None = None
    try:
        if source.is_file() and source.suffix.lower() == ".zip":
            tmp_root = Path(tempfile.mkdtemp(prefix="nymeria-ext-"))
            try:
                with zipfile.ZipFile(source) as archive:
                    archive.extractall(tmp_root)
            except zipfile.BadZipFile as exc:
                raise ServerBrowserError(f"{source} is not a zip file") from exc
            build_dir = _find_manifest_dir(tmp_root)
        elif source.is_dir():
            build_dir = _find_manifest_dir(source)
        else:
            build_dir = None
        if build_dir is None:
            raise ServerBrowserError(
                f"no extension build at {source} (expected a manifest.json in a "
                "build dir or inside a release zip)"
            )
        manifest = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("name") != "Nymeria Browser":
            raise ServerBrowserError(
                f"{build_dir / 'manifest.json'} is not the Nymeria Browser extension"
            )
        if ext_dir.exists():
            shutil.rmtree(ext_dir)
        shutil.copytree(build_dir, ext_dir)
        return str(manifest.get("version", ""))
    finally:
        if tmp_root is not None:
            shutil.rmtree(tmp_root, ignore_errors=True)


def _is_loopback_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"} or host.startswith("127.")


def transform_manifest(manifest: dict, base_url: str) -> dict:
    """The headless transform: optional host permissions become REQUIRED.

    Required permissions are auto-granted for an unpacked load, which is what
    replaces every popup click (the page-status grant included). The backend
    origin is added as well when it is neither loopback nor https: the shipping
    manifest only declares those two (a bearer token must not travel in clear
    to a remote host), and a server browser talking to its own host over
    `http://nymeria-api:8000` is the one case that rule does not serve.
    """
    out = dict(manifest)
    optional = list(out.pop("optional_host_permissions", []) or [])
    required = list(out.get("host_permissions", []) or [])
    merged = list(dict.fromkeys(required + optional))
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or ""
    if parsed.scheme == "http" and host and not _is_loopback_host(host):
        pattern = f"http://{host}/*"
        if pattern not in merged:
            merged.append(pattern)
    out["host_permissions"] = merged
    out["optional_host_permissions"] = []
    return out


def bake_config(
    ext_dir: Path,
    *,
    base_url: str,
    token: str,
    client_id: str,
    label: str = DEFAULT_LABEL,
    kind: str = KIND,
    log: Callable[[str], None] = print,
) -> Path:
    """Write the packaged config the extension adopts at startup (mode 0600).

    Atomic: the extension re-reads this file whenever it changes, so it must
    never observe a half-written one. The 0600 is measured, not assumed
    (:func:`enforce_mode`): the file carries an account token.
    """
    path = ext_dir / "config.json"
    payload = {
        "baseUrl": base_url.rstrip("/"),
        "token": token,
        "clientId": client_id,
        "kind": kind,
        "label": label,
    }
    write_atomic(path, json.dumps(payload) + "\n", mode=0o600)
    _harden(path, 0o600, "the baked extension config (it carries an account token)", log)
    return path


def read_baked_config(home: RigHome) -> dict[str, Any] | None:
    try:
        raw = json.loads(home.baked_config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


# --- sandbox probe and launch argv -------------------------------------------------------


@dataclass(frozen=True)
class SandboxDecision:
    no_sandbox: bool
    reason: str = ""


def probe_sandbox(
    binary: Path,
    *,
    run: Runner = subprocess.run,
    popen: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
    log: Callable[[str], None] = print,
) -> SandboxDecision:
    """Measure whether Chrome can build its sandbox here; fall back if not.

    One launch with the sandbox; only when that fails, one more with
    `--no-sandbox`. Both use a throwaway profile. Persisted by the caller so
    every later `run` reuses the answer instead of re-measuring.

    The probe is "Chrome comes up and opens its DevTools endpoint", the same
    readiness signal the rig itself lives on, NOT `--dump-dom`: measured on
    Chrome for Testing 152 and 153 in new headless, `--dump-dom` never
    returns on a server (about:blank, a data: URL, `--timeout` and
    `--virtual-time-budget` all hang until killed), which made every
    out-of-the-box `configure` spend 90 s timing out and then fail. A sandbox
    that cannot be built exits within a second with its reason on stderr,
    which is what the fallback branch keys on. ``run`` is still used for the
    library hints on total failure.
    """
    tmp = tempfile.mkdtemp(prefix="nymeria-sandbox-probe-")
    try:
        base = [
            str(binary),
            "--headless=new",
            "--no-first-run",
            "--disable-gpu",
            f"--user-data-dir={tmp}",
            "--remote-debugging-port=0",
            "about:blank",
        ]
        first = _probe_once(base, popen=popen)
        if first.ok:
            return SandboxDecision(no_sandbox=False)
        log("Chrome could not start with its sandbox here; trying --no-sandbox...")
        second = _probe_once(base + ["--no-sandbox"], popen=popen)
        if second.ok:
            return SandboxDecision(no_sandbox=True, reason=first.detail)
    finally:
        _remove_probe_profile(Path(tmp))
    raise ServerBrowserError(
        f"the browser could not start even without its sandbox: {second.detail[:400]}",
        hints=_library_hints_for(binary, run),
    )


def _remove_probe_profile(path: Path, *, attempts: int = 10, delay: float = 0.2) -> None:
    """Remove the probe's throwaway profile, tolerating Chrome's slow children.

    `_stop_probe` waits for the browser process only; the helpers it signalled
    (and the crashpad handler, which double-forks into its own session, so the
    group signal may not reach it) can still be writing `Crashpad/` under the
    profile for a moment, and the first rmtree then loses the race with
    ENOTEMPTY (seen while adopting a live rig: `configure` died in
    TemporaryDirectory's cleanup AFTER the probe had its answer). The answer
    is the point; the directory is retried briefly and then abandoned quietly,
    because a few KB left in the temp dir must never fail a configuration.
    """
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay)
    shutil.rmtree(path, ignore_errors=True)


@dataclass(frozen=True)
class _ProbeResult:
    ok: bool
    detail: str = ""


DEVTOOLS_READY_MARK = "DevTools listening on"


def _probe_once(
    argv: list[str],
    *,
    popen: Callable[..., "subprocess.Popen[bytes]"],
    timeout: float = 45.0,
) -> _ProbeResult:
    """Start Chrome, wait for its DevTools endpoint, stop it; report which happened.

    Success is `DevTools listening on` on stderr. Failure is the process
    exiting first (a sandbox that cannot be built does, with the reason as its
    last stderr lines) or the deadline passing. Whatever happened, the whole
    process GROUP is stopped: Chrome's zygote, GPU and crashpad helpers
    outlive the browser process, and a `run`-style kill of the parent alone
    left them writing the probe profile while it was being removed.
    """
    import threading

    spawn_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        spawn_kwargs["start_new_session"] = True  # pid == pgid, so killpg reaches the helpers
    try:
        proc = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=child_env(),
            **spawn_kwargs,
        )
    except OSError as exc:
        return _ProbeResult(False, str(exc))

    lines: list[str] = []
    ready = threading.Event()

    def pump() -> None:
        stream = proc.stderr
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            lines.append(line)
            if DEVTOOLS_READY_MARK in line:
                ready.set()

    reader = threading.Thread(target=pump, name="server-browser-probe-stderr", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    ok = False
    timed_out = False
    try:
        while True:
            if ready.is_set():
                ok = True
                break
            if proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
    finally:
        _stop_probe(proc)
    if ok:
        return _ProbeResult(True)
    # Chrome's helpers inherit the stderr pipe, so EOF arrives only once the
    # whole tree is down: join AFTER the stop, or this waits for nothing.
    reader.join(timeout=2)
    said = _probe_failure_summary(lines)
    if timed_out:
        detail = f"no DevTools endpoint after {timeout:.0f}s"
        return _ProbeResult(False, f"{detail}; last stderr: {said}" if said else detail)
    return _ProbeResult(False, said or f"exit {proc.returncode}")


_CHROME_LOG_PREFIX = re.compile(r"^\[[^\]]*\]\s*")
_CHROME_ERROR_LEVEL = re.compile(r"^\[[^\]]*:(FATAL|ERROR):")
_PROBE_KEY_WORDS = re.compile(r"sandbox|namespace|zygote|SUID|Operation not permitted", re.IGNORECASE)
_PROBE_NOISE = re.compile(r"^(#\d+ |\s*[a-z0-9]{2,3}: [0-9a-f]{16}|\[end of stack trace\]|Received signal)")


def _probe_failure_summary(lines: list[str]) -> str:
    """The one line worth persisting from a failed probe's stderr.

    A sandbox failure is a FATAL log line near the TOP of stderr ("No usable
    sandbox! ...") followed by a crash dump: numbered frames, register rows,
    `[end of stack trace]`. The last three lines are therefore registers,
    which is what `rig.json` and `nymeria browser status` used to show as the
    reason. Order of preference: the first FATAL line; else the first ERROR
    line whose MESSAGE names the sandbox machinery (never a WARNING, and never
    a match inside the `[pid:tid:date:LEVEL:file:line]` prefix: Chrome's GPU
    process routinely warns about `sandbox_linux.cc` while starting fine);
    else the last lines that are not crash-dump noise. Prefixes stripped.
    """
    cleaned = [line.strip() for line in lines if line.strip()]
    for line in cleaned:
        if ":FATAL:" in line:
            return _CHROME_LOG_PREFIX.sub("", line)[:400]
    for line in cleaned:
        if _CHROME_ERROR_LEVEL.match(line) and _PROBE_KEY_WORDS.search(_CHROME_LOG_PREFIX.sub("", line)):
            return _CHROME_LOG_PREFIX.sub("", line)[:400]
    tail = [_CHROME_LOG_PREFIX.sub("", line) for line in cleaned if not _PROBE_NOISE.match(line)]
    return " | ".join(tail[-3:])[:400]


def _stop_probe(proc: Any) -> None:
    """Stop the probe's Chrome and every helper it spawned, then reap it.

    Process-group (POSIX) or tree (Windows `taskkill /T`) signalling applies to
    a real ``subprocess.Popen`` only; anything else is a test double and gets
    its own ``terminate``/``kill``, so no unit test ever signals a real pid.
    """
    real = isinstance(proc, subprocess.Popen)
    if not real:
        proc.terminate()
    elif os.name == "nt":
        _terminate(proc.pid)  # taskkill /T: the whole tree
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass  # did not go quietly: escalate below
    if not real or os.name == "nt":
        proc.kill()
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # reaped by init eventually; nothing more this process can do


def build_chrome_argv(
    binary: Path,
    *,
    profile_dir: Path,
    ext_dir: Path,
    debug_port: int,
    user_agent: str,
    no_sandbox: bool,
    window_size: str = WINDOW_SIZE,
) -> list[str]:
    """The launch command. Never carries `--enable-automation` (sign-in trigger).

    `--remote-allow-origins` names the ONLY origin allowed to attach to the
    debugger: `devtools://devtools`, the desktop app's own DevTools frontend,
    which is what the human login handoff uses. No web origin belongs on that
    list. The origin allowlist and the loopback bind are separate controls
    solving separate problems: the bind stops other hosts reaching the port,
    the origin check stops WEB CONTENT in a browser on THIS host attaching to
    it, and this browser holds live signed-in sessions. "It is loopback-bound
    anyway" is not an argument for widening it (a page on an allowed origin
    only needs a target id to get full CDP). Never pair it with
    `--remote-debugging-address=0.0.0.0` either.
    """
    argv = [
        str(binary),
        "--headless=new",
        f"--user-agent={user_agent}",
        "--remote-allow-origins=devtools://devtools",
        f"--user-data-dir={profile_dir}",
        f"--disable-extensions-except={ext_dir}",
        f"--load-extension={ext_dir}",
        f"--remote-debugging-port={debug_port}",
        f"--window-size={window_size}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--noerrdialogs",
    ]
    if no_sandbox:
        argv.append("--no-sandbox")
    return argv


def pick_debug_port(preferred: int = DEFAULT_DEBUG_PORT) -> int:
    """``preferred`` when free, else the next free port above it."""
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise ServerBrowserError(f"no free debug port in {preferred}..{preferred + 49}")


# --- configure -----------------------------------------------------------------------


def configure(
    home: RigHome,
    *,
    base_url: str,
    token: str,
    client_id: str | None = None,
    label: str | None = None,
    source: str | Path | None = None,
    debug_port: int | None = None,
    sandbox: str = "auto",
    adopt_home: str | Path | None = None,
    download: Downloader | None = None,
    run: Runner = subprocess.run,
    popen: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
    log: Callable[[str], None] = print,
    platform_key: str | None = None,
) -> RigConfig:
    """Stage the extension, bake its config, decide the sandbox, write rig.json.

    Identity is STABLE across re-configures: an existing rig.json's client_id
    and debug port are reused unless overridden, so a token rotation or a port
    change never re-identifies the browser to the backend.

    A RUNNING rig is stopped before its extension is rewritten and restarted
    afterwards. The extension's service worker adopts the bake at BOOTSTRAP
    and the SSE fetch keeps that worker alive indefinitely, so a re-bake under
    a running browser is a silent no-op: the rig keeps using the old token and
    the old backend URL until something restarts it. Staging over the files it
    is running from also strands it, which is why
    the stop comes first.
    """
    if not base_url.strip() or not token.strip():
        raise ServerBrowserError("configure needs a base URL and an account token")
    base_url = validate_base_url(base_url)
    key = platform_key or cft_platform()
    binary = home.chrome_binary(key)
    if binary is None:
        raise ServerBrowserError(
            "Chrome for Testing is not installed here; run `nymeria browser install` first"
        )
    previous = RigConfig.load(home)
    home.path.mkdir(parents=True, exist_ok=True)
    _harden(home.path, 0o700, "the server-browser rig home", log)
    restart_after = _stop_for_reconfigure(home, previous, run=run, log=log)

    if source is None:
        source_path = fetch_extension_release(home, download=download, log=log)
        source_label = EXTENSION_RELEASE_TAG
    else:
        source_path = Path(source).expanduser()
        source_label = str(source_path)
    ext_version = stage_extension(source_path, home.ext_dir)
    manifest_path = home.ext_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = transform_manifest(manifest, base_url)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    resolved_id = validate_client_id(client_id) if client_id else (
        previous.client_id if previous else new_client_id()
    )
    resolved_label = clean_label(label) if label else (previous.label if previous else DEFAULT_LABEL)
    baked = bake_config(
        home.ext_dir,
        base_url=base_url,
        token=token.strip(),
        client_id=resolved_id,
        label=resolved_label,
        log=log,
    )

    if adopt_home:
        _adopt_profile(Path(adopt_home).expanduser(), home, log=log)
    home.profile_dir.mkdir(parents=True, exist_ok=True)
    _harden(home.profile_dir, 0o700, "the browser profile (it holds signed-in site sessions)", log)

    if debug_port is None:
        debug_port = previous.debug_port if previous else pick_debug_port()

    if sandbox == "on":
        decision = SandboxDecision(no_sandbox=False)
    elif sandbox == "off":
        decision = SandboxDecision(no_sandbox=True, reason="disabled by operator")
    else:
        log("Checking that the browser can start here...")
        decision = probe_sandbox(binary, run=run, popen=popen, log=log)
    if decision.no_sandbox:
        log(
            "WARNING: the browser will run with --no-sandbox "
            f"({decision.reason or 'sandbox unavailable'}). Ubuntu 23.10+ blocks "
            "unprivileged user namespaces through AppArmor; the durable fix is a "
            "root-installed AppArmor profile for the Chrome binary (see the "
            "server-browser deployment doc), after which `nymeria browser "
            "configure --sandbox on` re-enables it."
        )

    _drop_worker_script_cache(home, log=log)
    config = RigConfig(
        client_id=resolved_id,
        base_url=base_url,
        label=resolved_label,
        debug_port=debug_port,
        no_sandbox=decision.no_sandbox,
        sandbox_reason=decision.reason,
        extension_version=ext_version,
        extension_source=source_label,
        cft_version=_cft_version_from_binary(binary),
        configured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    config.save(home)
    log(
        f"Staged extension {ext_version or '(unknown version)'} at {home.ext_dir}; "
        f"baked config ({_actual_mode(baked)}); browser id {resolved_id}; "
        f"debug port {debug_port}."
    )
    if _retire_old_service_unit(previous, config, run=run, log=log):
        # The unit that would have been restarted no longer exists; the
        # retirement message names the command that installs its replacement.
        restart_after = None
    _restart_after_reconfigure(home, restart_after, log=log)
    return config


def _actual_mode(path: Path) -> str:
    """The mode the filesystem actually kept, for a log line that claims one."""
    if os.name == "nt":
        return "no POSIX mode on Windows"
    try:
        return f"{stat.S_IMODE(path.stat().st_mode):04o}"
    except OSError as exc:
        return f"mode unknown ({exc})"


def _browser_service_for(debug_port: int, *, runner: Runner) -> Any | None:
    """This rig's service manager, but only when its unit is actually installed."""
    from .service_install import ServiceUnavailableError, installed_artifact_path, service_manager

    spec = service_spec(debug_port)
    if installed_artifact_path(spec) is None:
        return None
    try:
        return service_manager(runner=runner, spec=spec)
    except ServiceUnavailableError:
        return None


def _stop_for_reconfigure(
    home: RigHome,
    previous: RigConfig | None,
    *,
    run: Runner,
    log: Callable[[str], None],
) -> Any | None:
    """Stop a running rig before its extension is rewritten under it.

    Returns what should bring it back: its service manager when a unit is
    installed, the string ``"foreground"`` when the rig was running without
    one, or None when nothing was running. The service unit is looked up by
    the PREVIOUS debug port, because that is the unit that is running now.
    """
    if running_pid(home, run=run) is None and not stray_chrome_pids(home, run=run):
        return None
    manager = _browser_service_for(
        previous.debug_port if previous else DEFAULT_DEBUG_PORT, runner=run
    )
    log("The server browser is running; stopping it so the new configuration takes effect.")
    try:
        for line in stop_browser(home, run=run):
            log(line)
    except ServerBrowserError as exc:
        log(f"WARNING: could not stop the running browser ({exc}).")
    return manager if manager is not None else "foreground"


def _retire_old_service_unit(
    previous: RigConfig | None,
    config: RigConfig,
    *,
    run: Runner,
    log: Callable[[str], None],
) -> bool:
    """Remove the service unit a debug-port change would otherwise orphan.

    A rig's unit name is keyed on its debug port (that is what is unique per
    rig on a multi-rig host), so `configure --debug-port 9333` renames the
    unit this rig WOULD install while the old one stays enabled and running.
    Both exec the same `browser run --root <root> --supervise`, read the same
    rig.json and drive the same profile: the loser hits the single-instance
    guard, exits non-zero, burns its restart budget, and nothing ever says
    why. Returns True when a unit was retired (or could not be).
    """
    if previous is None or previous.debug_port == config.debug_port:
        return False
    manager = _browser_service_for(previous.debug_port, runner=run)
    if manager is None:
        return False
    try:
        for line in manager.uninstall():
            log(line)
    except Exception as exc:  # noqa: BLE001 - a stuck uninstall must be loud, never fatal
        log(
            f"WARNING: the old browser service for debug port {previous.debug_port} could "
            f"not be removed ({exc}). Remove it by hand before installing the new one, or "
            "two units will fight over this profile."
        )
        return True
    log(
        f"Removed the browser service for debug port {previous.debug_port} (a unit is keyed "
        f"on its port, so the one for {config.debug_port} is a different unit). Install it "
        "with: nymeria browser service install"
    )
    return True


def _restart_after_reconfigure(
    home: RigHome, restart_after: Any | None, *, log: Callable[[str], None]
) -> None:
    """Bring a rig stopped by :func:`_stop_for_reconfigure` back up, or say so loudly.

    A silent no-op is the one unacceptable outcome here: an operator who ran
    `configure` and saw success must never be left with a browser still using
    the old bake, so every path either restarts the rig or prints the exact
    command that will.
    """
    if restart_after is None:
        return
    if restart_after != "foreground":
        try:
            restart_after.restart()
            log("Restarted the server browser service; the new configuration is live.")
            return
        except Exception as exc:  # noqa: BLE001 - a failed restart must be loud, never fatal
            log(f"WARNING: the browser service did not restart ({exc}).")
            log("Start it yourself: nymeria browser service restart")
            return
    log(
        "The server browser was stopped and is NOT running again: the new token, URL "
        "and extension take effect at its next start. Start it with: "
        f"nymeria browser run --home {home.path}"
    )


def _cft_version_from_binary(binary: Path) -> str:
    # <home>/cft/<version>/<platform dir>/... : the version dir is the ancestor
    # directly under cft/.
    for parent in binary.parents:
        if parent.parent.name == "cft":
            return parent.name
    return ""


WORKER_SCRIPT_CACHE_DIR = Path("Default") / "Service Worker"


def _drop_worker_script_cache(home: RigHome, *, log: Callable[[str], None]) -> None:
    """Remove the profile's cached service-worker scripts so the STAGED extension runs.

    Chrome keeps every service worker's script in the profile
    (`Default/Service Worker/ScriptCache`), keyed by origin, and the
    extension's origin is fixed by the key in its manifest. So a profile that
    last ran an older build keeps EXECUTING that build's worker after a
    restart, while `chrome.runtime.getManifest()` reports the new version from
    disk: measured on an adopted v0.28.0 profile, two Chrome starts ran the old
    worker (old identity, old token, no kind or label) and the new bake was
    never adopted; the third start, after an extension reload refreshed the
    cache, adopted it. The same mechanism hits an in-place upgrade. Cleared on
    every `configure`, after `_stop_for_reconfigure` (a stop it could not
    complete is already a printed WARNING, and a still-running Chrome would
    at worst repopulate the cache once). Cookies, Local Storage and the
    extension's own `chrome.storage.local` live in other profile
    subdirectories and are untouched; the directory also holds the Cache API
    storage of signed-in sites (`CacheStorage/`), which is a cache and is
    rebuilt. Chrome recreates the whole directory on the next start.
    """
    cache = home.profile_dir / WORKER_SCRIPT_CACHE_DIR
    if not cache.exists():
        return
    try:
        shutil.rmtree(cache)
    except OSError as exc:
        log(
            f"WARNING: could not clear the profile's service-worker cache at {cache} "
            f"({exc}); if the rig keeps announcing an old extension version, remove it "
            "by hand with the rig stopped."
        )
        return
    log("Cleared the profile's cached service-worker scripts (the staged extension runs, not a previous build).")


def _adopt_profile(old_home: Path, home: RigHome, *, log: Callable[[str], None]) -> None:
    """Reuse an existing rig's profile (signed-in sessions) for this rig."""
    old_profile = old_home / "profile" if (old_home / "profile").is_dir() else old_home
    if not (old_profile / "Default").is_dir() and not (old_profile / "Local State").exists():
        raise ServerBrowserError(f"{old_home} does not look like a Chrome profile or rig home")
    if home.profile_dir.exists() and any(home.profile_dir.iterdir()):
        raise ServerBrowserError(
            f"{home.profile_dir} already has a profile; remove it first or skip --adopt-home"
        )
    if old_profile.resolve() == home.profile_dir.resolve():
        return
    log(f"Adopting the existing profile from {old_profile} (copy; the original is left alone)...")
    shutil.rmtree(home.profile_dir, ignore_errors=True)
    shutil.copytree(old_profile, home.profile_dir, symlinks=True)


# --- pid guard -----------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process? Never uses os.kill on Windows (it terminates)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            still_active = 259
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_rig_chrome_cmdline(home: RigHome, cmdline: str) -> bool:
    """Is ``cmdline`` THIS rig's browser (and not one of its helper processes)?

    Two anchors, both required. The profile flag alone matches any process
    that merely MENTIONS the path (`grep --user-data-dir=/rig/profile` did),
    and this predicate decides what `stop` signals. The binary alone matches a
    second rig sharing the same install. `--type=` drops Chrome's own renderer
    and GPU children: they die with their parent, and they are not the process
    the pidfile names.
    """
    if not cmdline:
        return False
    if not cmdline.startswith(f"{home.cft_dir}{os.sep}"):
        return False
    return f"--user-data-dir={home.profile_dir}" in cmdline and "--type=" not in cmdline


def _pid_cmdline(pid: int, *, run: Runner = subprocess.run) -> str:
    """The full command line of ``pid``, or "" when it cannot be read."""
    if os.name == "nt":
        return ""
    try:
        result = run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=15,
            env=child_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if text:
            return text
    return ""


def _pid_file_pid(home: RigHome) -> int | None:
    """The pid recorded in the guard file, alive or not."""
    try:
        return int(home.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def running_pid(home: RigHome, *, run: Runner = subprocess.run) -> int | None:
    """The guard file's process when it is alive AND is this rig's browser.

    Liveness alone is not enough, and the gap is dangerous rather than
    cosmetic: after a reboot or a SIGKILL the guard file survives naming a
    dead pid, the kernel hands that number to something else, and `stop` would
    then SIGTERM/SIGKILL whatever inherited it (on a Nymeria host: the API,
    the worker, or postgres). So the pid must also LOOK like this rig's
    browser. The argv read is POSIX (`ps`); on Windows there is no exec path,
    the supervising `run` rewrites the file at every launch and clears it on
    exit, and there is no portable argv read, so liveness is all there is.
    """
    pid = _pid_file_pid(home)
    if pid is None or not pid_alive(pid):
        return None
    if os.name == "nt":
        return pid
    return pid if is_rig_chrome_cmdline(home, _pid_cmdline(pid, run=run)) else None


def _write_pid(home: RigHome, pid: int) -> None:
    home.path.mkdir(parents=True, exist_ok=True)
    write_atomic(home.pid_file, f"{pid}\n", mode=0o600)


def _claim_pid_file(home: RigHome, pid: int) -> None:
    """Take the single-instance guard for ``pid``, atomically.

    O_EXCL IS the guard: check-then-write let two concurrent
    `nymeria browser run` calls both read "nothing running", both sweep for
    strays, and both launch Chrome on one profile, leaving only Chrome's own
    SingletonLock between them and a pidfile naming whichever wrote last. A
    guard file whose pid is DEAD is stale (a reboot, a SIGKILL, or the exec
    path, which by construction cannot unlink its own guard) and is replaced;
    a live one refuses, without signalling anything.
    """
    home.path.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(home.pid_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            holder = _pid_file_pid(home)
            if holder is not None and pid_alive(holder):
                raise ServerBrowserError(
                    f"a server browser from {home.path} is already running (pid {holder}); "
                    "use `nymeria browser stop` first"
                ) from None
            home.pid_file.unlink(missing_ok=True)
            continue
        except OSError as exc:
            raise ServerBrowserError(
                f"could not write the single-instance guard {home.pid_file}: {exc}"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{pid}\n")
        return
    raise ServerBrowserError(
        f"could not take the single-instance guard at {home.pid_file} (it is being "
        "created and removed concurrently); retry"
    )


def stop_requested(home: RigHome) -> bool:
    """Has a `stop` asked a supervising `run` to stay down?"""
    return home.stop_request.exists()


def request_stop(home: RigHome) -> None:
    """Ask a supervising `run` not to restart the browser it is about to lose.

    Every installed unit and scheduled task runs `--supervise`, so killing
    Chrome alone is not a stop: the supervisor sees a non-zero exit and starts
    it again, while `stop` reports success. The flag is cleared by the next
    `run`, never by `stop`, because the supervisor may not have read it yet.
    """
    try:
        home.path.mkdir(parents=True, exist_ok=True)
        home.stop_request.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    except OSError:
        # Best effort: a read-only rig home still stops the browser itself.
        pass


def clear_stop_request(home: RigHome) -> None:
    home.stop_request.unlink(missing_ok=True)


def stray_chrome_pids(home: RigHome, *, run: Runner = subprocess.run) -> list[int]:
    """This rig's Chrome processes, whether or not the pidfile knows them.

    Exists because a stale twin once survived a hand-rolled kill pattern and kept
    executing every command beside its replacement (2026-08-28). POSIX only;
    on Windows the supervising `run` owns the child and there is no exec.
    """
    if os.name == "nt":
        return []
    try:
        result = run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=15,
            env=child_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not is_rig_chrome_cmdline(home, parts[1]):
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            continue
    return pids


# --- run / stop ---------------------------------------------------------------------------


def launch_plan(home: RigHome, *, run: Runner = subprocess.run, platform_key: str | None = None) -> tuple[RigConfig, list[str]]:
    """Validate the rig and return (config, chrome argv) without launching."""
    key = platform_key or cft_platform()
    config = RigConfig.load(home)
    if config is None:
        raise ServerBrowserError(
            f"no server browser configured at {home.path}; run `nymeria browser configure`"
        )
    binary = home.chrome_binary(key)
    if binary is None:
        raise ServerBrowserError("Chrome for Testing is not installed; run `nymeria browser install`")
    if not (home.ext_dir / "manifest.json").is_file() or not home.baked_config.is_file():
        raise ServerBrowserError("the extension is not staged; run `nymeria browser configure`")
    version = verify_binary(binary, run=run)
    major = int(version.split(".", 1)[0])
    argv = build_chrome_argv(
        binary,
        profile_dir=home.profile_dir,
        ext_dir=home.ext_dir,
        debug_port=config.debug_port,
        user_agent=headful_user_agent(major, key),
        no_sandbox=config.no_sandbox,
    )
    return config, argv


def run_browser(
    home: RigHome,
    *,
    fresh_profile: bool = False,
    supervise: bool = False,
    run: Runner = subprocess.run,
    popen: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
    exec_fn: Callable[[str, list[str], dict[str, str]], Any] = os.execve,
    log: Callable[[str], None] = print,
    platform_key: str | None = None,
    max_restarts: int = 20,
    healthy_run_seconds: float = HEALTHY_RUN_SECONDS,
) -> int:
    """Launch the browser in the foreground (a service manager owns the lifecycle).

    Two modes, and note which one the installed service uses. Default: ``exec``
    into Chrome on POSIX so this process IS the browser (the guard file is
    claimed with our pid first, which exec preserves), which is what a bare
    `nymeria browser run` in a terminal does. ``supervise`` (what
    `service_exec_argv` passes, so EVERY installed unit and scheduled task runs
    this branch on every platform): keep Chrome as a child and restart it on
    abnormal exit with backoff. Chrome exits on its own for reasons a restart
    fixes (a renderer OOM, a GPU process crash), and a scheduled task has no
    crash supervision at all, so the supervised branch is the service default
    rather than the exception.

    Both modes hand Chrome :func:`child_env`, never this process's own
    environment: `run.py` loads the deployment env for every subcommand, so by
    the time we get here `os.environ` holds the vault key, the service token,
    the database passwords and every provider key, and Chrome is the process
    that loads arbitrary web content, an extension, and an unauthenticated CDP
    port.
    """
    clear_stop_request(home)
    config, argv = launch_plan(home, run=run, platform_key=platform_key)
    # The guard is taken before the stray sweep, so a concurrent `run` cannot
    # slip between that sweep and the launch.
    _claim_pid_file(home, os.getpid())
    exec_taken = False
    try:
        strays = [p for p in stray_chrome_pids(home, run=run) if p != os.getpid()]
        if strays:
            raise ServerBrowserError(
                f"Chrome is already using this profile (pid {', '.join(map(str, strays))}) "
                "outside the pidfile; run `nymeria browser stop` to clear it"
            )
        if fresh_profile and home.profile_dir.exists():
            log(
                f"Removing the profile dir {home.profile_dir} (stale service-worker cache "
                "guard; site sessions are lost; the baked config re-adopts on first run)."
            )
            shutil.rmtree(home.profile_dir)
        home.profile_dir.mkdir(parents=True, exist_ok=True)
        _harden(home.profile_dir, 0o700, "the browser profile (it holds signed-in site sessions)", log)
        if config.no_sandbox:
            log(
                "WARNING: running with --no-sandbox (measured at configure: "
                f"{config.sandbox_reason or 'sandbox unavailable'})."
            )
        log(
            f"Launching the server browser on debug port {config.debug_port} "
            f"(profile {home.profile_dir})."
        )
        if os.name != "nt" and not supervise:
            # exec keeps our pid, so the guard we already hold names Chrome
            # itself from here on. Nothing of ours runs afterwards to clear
            # it: that is why `running_pid` verifies the pid's argv rather
            # than trusting a file no exit path can clean up.
            exec_taken = True
            exec_fn(argv[0], argv, child_env())
            return 0  # pragma: no cover - exec does not return

        env = child_env()
        restarts = 0
        delay = 2.0
        while True:
            started = time.monotonic()
            child = popen(argv, env=env)
            _write_pid(home, child.pid)
            code = child.wait()
            if stop_requested(home):
                log("A stop was requested; not restarting the browser.")
                return 0
            if not supervise or code == 0:
                return int(code or 0)
            if time.monotonic() - started >= healthy_run_seconds:
                # A run that stayed up is not part of a crash loop: without
                # this a long-lived service spends its whole budget on
                # unrelated crashes weeks apart and then stays down.
                restarts = 0
                delay = 2.0
            restarts += 1
            if restarts > max_restarts:
                log(f"The browser exited {code} and the restart budget is spent; giving up.")
                return int(code)
            log(f"The browser exited {code}; restarting in {delay:.0f}s (attempt {restarts}).")
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            if stop_requested(home):
                log("A stop was requested; not restarting the browser.")
                return 0
    finally:
        if not exec_taken:
            home.pid_file.unlink(missing_ok=True)


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=30,
            env=child_env(),
        )
        return
    import signal

    # PermissionError: the pid was recycled and now belongs to another user.
    # `running_pid` and the stray sweep both verify argv before anything gets
    # here, so this is the last line rather than the only one, but it must not
    # be a traceback out of `nymeria browser stop`.
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return


def _kill(pid: int) -> None:
    if os.name == "nt":
        _terminate(pid)
        return
    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def stop_browser(
    home: RigHome,
    *,
    run: Runner = subprocess.run,
    wait_seconds: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Stop the rig's browser: the pidfile's process plus any stray on its profile.

    Signals only processes VERIFIED to be this rig's Chrome, and raises the
    stop flag first: every installed unit runs `--supervise`, so the pidfile
    holds the Chrome CHILD and killing it alone just hands the rig back to its
    own supervisor, which restarts it while this function reports success.
    """
    targets: list[int] = []
    known = running_pid(home, run=run)
    if known is not None:
        targets.append(known)
    for pid in stray_chrome_pids(home, run=run):
        if pid not in targets and pid != os.getpid():
            targets.append(pid)
    if not targets:
        home.pid_file.unlink(missing_ok=True)
        return [f"No server browser from {home.path} is running."]
    request_stop(home)
    for pid in targets:
        _terminate(pid)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline and any(pid_alive(p) for p in targets):
        sleep(0.25)
    survivors = [p for p in targets if pid_alive(p)]
    for pid in survivors:
        _kill(pid)
    if survivors:
        sleep(0.5)
    still = [p for p in targets if pid_alive(p)]
    home.pid_file.unlink(missing_ok=True)
    if still:
        raise ServerBrowserError(
            f"could not stop the browser (pid {', '.join(map(str, still))} survived SIGKILL)"
        )
    return [
        f"Stopped the server browser (pid {', '.join(map(str, targets))}).",
        "A supervising run (every installed service) sees the stop and stays down: "
        "start it again with `nymeria browser service restart` or `nymeria browser run`.",
    ]


# --- status -----------------------------------------------------------------------------


@dataclass
class RigStatus:
    home: Path
    installed: bool = False
    configured: bool = False
    running: bool = False
    pid: int | None = None
    chrome_version: str = ""
    extension_worker_present: bool | None = None
    backend_reachable: bool | None = None
    token_valid: bool | None = None
    identity: str = ""
    connected_per_backend: bool | None = None
    no_sandbox: bool = False
    debug_port: int | None = None
    client_id: str = ""
    label: str = ""
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        # extension_worker_present is deliberately NOT part of this: an MV3
        # service worker that has idle-stopped is absent and perfectly
        # healthy, so the backend's view of the connection is the truth (see
        # the line status() prints beside it).
        return (
            self.installed
            and self.configured
            and self.running
            and self.token_valid is not False
            and self.connected_per_backend is not False
        )


def connected_in_roster(rows: Any, client_id: str) -> bool | None:
    """Is ``client_id`` connected, per `/browser list`'s roster rows?

    True or False when the backend knows this browser, None when it does not
    (or the payload is not a roster). The rows are DISPLAY STRINGS built by
    `core.browser_targets.roster_lines`, one per known browser:

        "server browser (a1b2c3d4, server): connected, v0.29.0"
        "a1b2c3d4 (server): disconnected 12s ago"

    So the id in a row is the SHORT one: the first eight characters after the
    `nymeria-browser-` prefix (`browser_targets.short_browser_id`), not the
    last eight of the full id, and the state is the tail after the final
    ": ". Parsing these as dicts (they never were) is what left `nymeria
    browser status` permanently saying "not in the roster yet" and left the
    Docker install's account-default step waiting on a field that could never
    become True.
    """
    if not isinstance(rows, list) or not client_id:
        return None
    short = (
        client_id[len(CLIENT_ID_PREFIX):][:8]
        if client_id.startswith(CLIENT_ID_PREFIX)
        else client_id[:8]
    )
    if not short:
        return None
    for row in rows:
        if not isinstance(row, str) or (short not in row and client_id not in row):
            continue
        state = row.rsplit(": ", 1)[-1].strip().lower()
        return state.startswith("connected")
    return None


def http_json(url: str, *, token: str | None = None, method: str = "GET", body: dict | None = None, timeout: float = 4.0) -> Any:
    headers = {"User-Agent": _HTTP_UA, "Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8") or "null")


def status(
    home: RigHome,
    *,
    http_json: Callable[..., Any] = http_json,
    probe_backend: bool = True,
    platform_key: str | None = None,
) -> RigStatus:
    """What is true about this rig right now: local facts plus the backend's view."""
    report = RigStatus(home=home.path)
    try:
        key = platform_key or cft_platform()
        binary = home.chrome_binary(key)
    except ServerBrowserError:
        binary = None
    report.installed = binary is not None
    if binary is not None:
        report.lines.append(f"Chrome for Testing: {binary}")
    else:
        report.problems.append("Chrome for Testing is not installed (nymeria browser install).")
    try:
        config = RigConfig.load(home)
    except ServerBrowserError as exc:
        # A status report names every failure rather than raising one: doctor
        # and the CLI both render this.
        report.problems.append(str(exc))
        report.problems.extend(exc.hints)
        return report
    report.configured = config is not None and home.baked_config.is_file()
    if config is None:
        report.problems.append("Not configured (nymeria browser configure).")
        return report
    report.debug_port = config.debug_port
    report.client_id = config.client_id
    report.label = config.label
    report.no_sandbox = config.no_sandbox
    report.lines.append(
        f"Browser id {config.client_id} ({config.label}); extension {config.extension_version or '?'}; "
        f"debug port {config.debug_port}"
    )
    if config.no_sandbox:
        report.lines.append(
            f"Sandbox: OFF (--no-sandbox; {config.sandbox_reason or 'measured unavailable at configure'})"
        )
    report.pid = running_pid(home)
    try:
        version = http_json(f"http://127.0.0.1:{config.debug_port}/json/version")
        report.running = True
        report.chrome_version = str(version.get("Browser", ""))
        report.lines.append(f"Running: {report.chrome_version} (pid {report.pid or '?'})")
        targets = http_json(f"http://127.0.0.1:{config.debug_port}/json")
        prefix = f"chrome-extension://{EXTENSION_ID}/"
        present = any(
            t.get("type") == "service_worker" and str(t.get("url", "")).startswith(prefix)
            for t in targets or []
        )
        report.extension_worker_present = present
        report.lines.append(
            "Extension worker: present"
            if present
            else "Extension worker: absent (idle-stopped is normal; the backend view below is the truth)"
        )
    except Exception as exc:  # noqa: BLE001 - a status report must name every failure, not raise
        report.running = False
        if report.pid:
            report.problems.append(
                f"pid {report.pid} is alive but the debug port {config.debug_port} did not answer ({exc})."
            )
        else:
            report.problems.append("Not running (nymeria browser service restart, or nymeria browser run).")
    if not probe_backend:
        return report
    baked = read_baked_config(home) or {}
    token = str(baked.get("token") or "")
    base_url = config.base_url.rstrip("/")
    try:
        http_json(f"{base_url}/health")
        report.backend_reachable = True
    except Exception as exc:  # noqa: BLE001
        report.backend_reachable = False
        report.problems.append(f"Backend not reachable at {base_url} ({exc}).")
        return report
    try:
        me = http_json(f"{base_url}/me", token=token)
        report.token_valid = True
        report.identity = str(me.get("email") or me.get("id") or "")
        report.lines.append(f"Backend: {base_url} as {report.identity}")
    except Exception as exc:  # noqa: BLE001
        report.token_valid = False
        report.problems.append(
            f"The baked token was rejected by {base_url} ({exc}); re-run `nymeria browser configure` "
            "with a fresh token."
        )
        return report
    try:
        roster = http_json(
            f"{base_url}/commands/execute",
            token=token,
            method="POST",
            body={"command": "/browser list", "surface": "cli"},
        )
        rows = ((roster or {}).get("data") or {}).get("browsers")
        connected = connected_in_roster(rows, config.client_id)
        report.connected_per_backend = connected
        if connected is True:
            report.lines.append("Backend view: this browser is connected.")
        elif connected is False:
            report.problems.append(
                "Backend view: this browser is known but NOT connected (the extension "
                "worker may be mid-reconnect; retry in a minute)."
            )
        else:
            report.lines.append("Backend view: this browser is not in the roster yet.")
    except Exception as exc:  # noqa: BLE001
        report.lines.append(f"Backend roster unavailable ({exc}).")
    return report


# --- service ----------------------------------------------------------------------------------


def service_spec(debug_port: int):
    """The service identity for this rig; one unit per rig on multi-instance hosts."""
    from .service_install import ServiceSpec

    return ServiceSpec(
        systemd_unit=f"nymeria-browser-{debug_port}.service",
        launchd_label=f"com.nymeria.browser.{debug_port}",
        windows_task_name=f"NymeriaOS Server Browser {debug_port}",
        description=f"Nymeria server browser (debug port {debug_port})",
        syslog_identifier="nymeria-browser",
        log_basename=f"server-browser-{debug_port}",
        install_hint="nymeria browser service install",
    )


def service_exec_argv(root: Path) -> list[str]:
    from .service_install import resolve_exec_argv

    return resolve_exec_argv(("browser", "run", "--root", str(root), "--supervise"))


def browser_service_manager(home: RigHome, *, runner: Runner = subprocess.run):
    """The platform service manager bound to this rig's spec."""
    from .service_install import service_manager

    config = RigConfig.load(home)
    if config is None:
        raise ServerBrowserError(
            "configure the server browser before installing its service"
        )
    return service_manager(runner=runner, spec=service_spec(config.debug_port))


# --- provisioning (the wizard's one call) ---------------------------------------------------------


@dataclass
class ProvisionReport:
    ok: bool
    config: RigConfig | None = None
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manual_commands: list[str] = field(default_factory=list)
    service_installed: bool = False


def provision(
    root: Path,
    *,
    base_url: str,
    token: str,
    client_id: str | None = None,
    label: str = DEFAULT_LABEL,
    home: RigHome | None = None,
    source: str | Path | None = None,
    install_service: bool = True,
    log: Callable[[str], None] = print,
    run: Runner = subprocess.run,
    popen: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
    download: Downloader | None = None,
    fetch: Fetcher | None = None,
    platform_key: str | None = None,
) -> ProvisionReport:
    """Install, configure, and (where possible) start the server browser as a service.

    Never raises: setup must finish whatever happens here, so every failure
    becomes a warning plus the manual commands that would finish the job. That
    means EVERY exception, not just ``ServerBrowserError``: the paths under
    here also `rmtree`, `copytree`, `mkdir`, `rename` and write a Windows
    shim, and an `OSError(28, "No space left on device")` out of `nymeria init`
    is exactly the failure this promise exists to prevent.
    """
    rig = home or resolve_rig_home(root)
    report = ProvisionReport(ok=False)
    manual_configure = (
        f"nymeria browser configure --root {root} --base-url {base_url} --token-file <file>"
    )
    try:
        installed = install(rig, fetch=fetch, download=download, run=run, log=log, platform_key=platform_key)
        report.lines.append(
            f"Chrome for Testing {installed.version} "
            + ("already present." if installed.already_installed else "installed.")
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring: this never raises
        report.warnings.append(f"Could not install Chrome for Testing: {exc}")
        report.warnings.extend(getattr(exc, "hints", ()))
        report.manual_commands.extend([f"nymeria browser install --root {root}", manual_configure])
        return report
    try:
        config = configure(
            rig,
            base_url=base_url,
            token=token,
            client_id=client_id,
            label=label,
            source=source,
            run=run,
            popen=popen,
            log=log,
            download=download,
            platform_key=platform_key,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring: this never raises
        report.warnings.append(f"Could not configure the server browser: {exc}")
        report.warnings.extend(getattr(exc, "hints", ()))
        report.manual_commands.append(manual_configure)
        return report
    report.config = config
    report.ok = True
    if config.no_sandbox:
        report.warnings.append(
            "The browser runs with --no-sandbox here (user namespaces are restricted); "
            "`nymeria doctor` keeps reporting it."
        )
    if not install_service:
        report.manual_commands.append(f"nymeria browser run --root {root}")
        return report
    from .service_install import ServiceUnavailableError

    try:
        manager = browser_service_manager(rig, runner=run)
        install_report = manager.install(exec_argv=service_exec_argv(root), root=root)
        report.service_installed = True
        report.lines.extend(install_report.lines)
        report.warnings.extend(install_report.warnings)
        report.lines.extend(install_report.notes)
    except ServiceUnavailableError as exc:
        report.warnings.append(f"No background service for the browser here: {exc}")
        report.warnings.extend(exc.hints)
        report.manual_commands.append(f"nymeria browser run --root {root}")
    except Exception as exc:  # noqa: BLE001 - see the docstring: this never raises
        report.warnings.append(f"Could not install the browser service: {exc}")
        report.warnings.extend(getattr(exc, "hints", ()))
        report.manual_commands.append(f"nymeria browser service install --root {root}")
    return report


# --- CLI ----------------------------------------------------------------------------------------------


def _print_error(exc: ServerBrowserError) -> None:
    print(f"Server browser: {exc}")
    for hint in exc.hints:
        print(f"  - {hint}")


def browser_cli(args: Any) -> int:
    """Back `nymeria browser <action> ...`. Returns the exit code."""
    from ._runtime_paths import configure_project_root

    root_arg = getattr(args, "root", None)
    root = Path(root_arg).expanduser().resolve() if root_arg else configure_project_root()
    # An explicit --root answers for THAT root alone. run.py merges the launch
    # root's env files into os.environ at startup, so an env-first lookup here
    # made `nymeria browser configure --root <B>` (the wizard's own printed
    # Docker remediation) resolve install A's rig and re-bake it with B's URL
    # and token. Without --root the launch root IS the target, and a shell
    # export is a deliberate override, so the env keeps winning there.
    home = resolve_rig_home(root, getattr(args, "home", None), process_env=root_arg is None)
    action = getattr(args, "action", None) or "status"
    try:
        if action == "install":
            install(home)
            return 0
        if action == "configure":
            token = _token_from_args(args)
            base_url = getattr(args, "base_url", None) or f"http://localhost:{resolve_api_port(root)}"
            configure(
                home,
                base_url=base_url,
                token=token,
                client_id=getattr(args, "client_id", None),
                label=getattr(args, "label", None),
                source=getattr(args, "source", None),
                debug_port=getattr(args, "debug_port", None),
                sandbox=getattr(args, "sandbox", None) or "auto",
                adopt_home=getattr(args, "adopt_home", None),
            )
            # A rig at a non-default home is otherwise invisible to the next
            # `nymeria init`, which would provision a SECOND one beside it.
            write_rig_home_pointer(root, home, log=print)
            return 0
        if action == "run":
            return run_browser(
                home,
                fresh_profile=bool(getattr(args, "fresh_profile", False)),
                supervise=bool(getattr(args, "supervise", False)),
            )
        if action == "stop":
            for line in stop_browser(home):
                print(line)
            return 0
        if action == "status":
            return _cli_status(home)
        if action == "service":
            return _cli_service(home, root, getattr(args, "service_action", None) or "status")
    except ServerBrowserError as exc:
        _print_error(exc)
        return 1
    print(f"Unknown browser action: {action}")
    return 2


def _token_from_args(args: Any) -> str:
    token_file = getattr(args, "token_file", None)
    if token_file:
        try:
            return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ServerBrowserError(f"could not read --token-file: {exc}") from exc
    token = getattr(args, "token", None)
    if token:
        print(
            "Note: a token on the command line lands in shell history and process "
            "listings; prefer --token-file."
        )
        return str(token)
    # Only ever read stdin when it was ASKED for. Reading it whenever stdin is
    # not a tty made `configure` inside a pipeline (or any non-interactive
    # runner) swallow unrelated input and treat it as the account token.
    if getattr(args, "token_stdin", False):
        data = sys.stdin.read().strip()
        if data:
            return data
        raise ServerBrowserError("--token-stdin was given but stdin held no token")
    raise ServerBrowserError(
        "configure needs an account token: --token-file <path>, --token-stdin, or --token"
    )


def _cli_status(home: RigHome) -> int:
    report = status(home)
    print(f"Server browser home: {report.home}")
    for line in report.lines:
        print(f"  {line}")
    for problem in report.problems:
        print(f"  ! {problem}")
    return 0 if report.healthy else 1


def _cli_service(home: RigHome, root: Path, action: str) -> int:
    from .service_install import ServiceInstallError, ServiceUnavailableError

    try:
        manager = browser_service_manager(home)
    except ServiceUnavailableError as exc:
        print(f"Background service unavailable: {exc}")
        for hint in exc.hints:
            print(f"  - {hint}")
        print(f"Run the browser in the foreground instead: nymeria browser run --root {root}")
        return 2
    if action == "install":
        try:
            launch_plan(home)
        except ServerBrowserError as exc:
            _print_error(exc)
            return 2
        try:
            report = manager.install(exec_argv=service_exec_argv(root), root=root)
        except ServiceUnavailableError as exc:
            print(f"Cannot install a background service here: {exc}")
            for hint in exc.hints:
                print(f"  - {hint}")
            return 2
        except (ServiceInstallError, OSError) as exc:
            print(f"Service install failed: {exc}")
            return 1
        for line in report.lines:
            print(line)
        for warning in report.warnings:
            print(f"Warning: {warning}")
        for note in report.notes:
            print(note)
        return 0
    if action == "uninstall":
        try:
            for line in manager.uninstall():
                print(line)
        except (ServiceInstallError, OSError) as exc:
            print(f"Uninstall failed: {exc}")
            return 1
        return 0
    if action == "restart":
        try:
            manager.restart()
        except (ServiceUnavailableError, ServiceInstallError) as exc:
            print(f"Restart failed: {exc}")
            for hint in getattr(exc, "hints", ()):
                print(f"  - {hint}")
            return 1
        print("Server browser service restarted.")
        return 0
    if action == "status":
        state = manager.status()
        print(f"{manager.name}: {state.detail} ({manager.artifact_path})")
        return 0 if state.running else 1
    print(f"Unknown service action: {action}")
    return 2


__all__ = [
    "CLIENT_ID_PREFIX",
    "DEFAULT_DEBUG_PORT",
    "DEFAULT_LABEL",
    "EXTENSION_ID",
    "EXTENSION_RELEASES_URL",
    "EXTENSION_RELEASE_SHA256",
    "EXTENSION_RELEASE_TAG",
    "HOME_ENV_KEY",
    "KIND",
    "TOKEN_LABEL",
    "InstallResult",
    "ProvisionReport",
    "RigConfig",
    "RigHome",
    "RigStatus",
    "SandboxDecision",
    "ServerBrowserError",
    "bake_config",
    "browser_cli",
    "browser_service_manager",
    "build_chrome_argv",
    "cft_platform",
    "chrome_relative_binary",
    "MAC_PLATFORM_KEYS",
    "clean_label",
    "clear_stop_request",
    "configure",
    "connected_in_roster",
    "default_rig_home",
    "enforce_mode",
    "extension_release_url",
    "fetch_extension_release",
    "headful_user_agent",
    "http_json",
    "install",
    "is_rig_chrome_cmdline",
    "launch_plan",
    "missing_library_hint",
    "missing_shared_libraries",
    "new_client_id",
    "parse_chrome_version",
    "pick_debug_port",
    "probe_sandbox",
    "provision",
    "read_baked_config",
    "read_rig_home_pointer",
    "request_stop",
    "resolve_api_port",
    "resolve_rig_home",
    "rig_home_pointer",
    "resolve_stable_download",
    "run_browser",
    "running_pid",
    "service_exec_argv",
    "service_spec",
    "stage_extension",
    "status",
    "stop_browser",
    "stop_requested",
    "stray_chrome_pids",
    "transform_manifest",
    "validate_base_url",
    "validate_client_id",
    "verify_binary",
    "write_atomic",
    "write_rig_home_pointer",
]
