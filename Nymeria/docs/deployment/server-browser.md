# The server browser

The server browser is a headless Chrome that runs beside your Nymeria
backend with the Nymeria browser extension loaded and already connected to
your account. It gives the assistant a browser of its own, so the
`chrome_*` tools work on a fresh install instead of refusing.

`nymeria init` offers to install it (default yes) and the `nymeria browser`
command manages it afterwards. Everything here is optional: an install that
skips it downloads nothing and changes no backend behavior, and the
`chrome_*` tools then wait for you to connect the extension in your own
Chrome. A skip never removes a rig you already have: setup keeps it and
keeps `SERVER_BROWSER_HOME` pointing at it, because the key has to stay
true. Nor does declining have to be re-declined: only a FIRST run defaults
to install. A reconfigure defaults to what is true on disk, install when
this install has a rig (a re-run re-bakes the same one, identity intact) and
skip when it has none, so a later `nymeria init --non-interactive` to change
something else does not silently provision a browser you turned down.

## Why it exists

The `chrome_*` tools drive a real Chrome through the
[nymeria-browser](https://github.com/ManningAskew7/nymeria-browser)
extension, and `browser-control` has been one of the default-on kits since
2026-08-30, so browsing is a capability every fresh account already has. On
a fresh install nobody had installed the extension anywhere, so the first
browsing request answered with "no extension connected. Open the extension
popup and click Connect", naming a popup the user had never seen. Getting
one meant downloading a build, loading an unpacked extension in developer
mode, minting a token by hand, and pasting both into that popup.

The server browser removes that first step entirely. Setup downloads the
browser and the extension, wires them to the local API as you, and runs the
result as a background service.

## The two kinds of browser

Every browser on your account carries a `kind`, shown by
`chrome_browsers`, `chrome_target`, the tab-free `chrome_health` probe and
`/browser list`:

| | `server` | `desktop` |
|---|---|---|
| What | The server browser: headless Chrome beside the backend | The extension in a Chrome you can see |
| Signed in to | Nothing, until a human signs it in (see below) | Whatever you are signed in to |
| Can you watch it | No; the agent's narration is your only view | Yes |
| When it is down | `nymeria browser status` on the host | The extension popup in your Chrome |

`desktop` is the default for any browser that does not announce a kind, so
every extension installed before this feature reads as `desktop`.

**Kind never decides routing.** Commands follow the existing target ladder
(the thread's `chrome_target` override, then your account default, then the
single connected browser, then a refusal listing the roster) whatever kinds
are connected. Kind is information for rosters, refusals and the kit's
guidance only. What it does change is the advice: a refusal about a server
browser points at `nymeria browser status` and never at a popup it does not
have.

At install the server browser is set as your account's default target, so
it keeps the wheel when you later connect your own Chrome. Switch a single
thread with `chrome_target`, or change the account default with `/browser
default`.

## What the wizard does

`nymeria init` shows one screen after the skill kits: install or skip,
pre-selected on what is true today (install on a first run, or on a
reconfigure that finds a rig; skip on a reconfigure that finds none). A
`--quick` or `--non-interactive` run applies that same default without
showing the screen, and `--no-server-browser` forces skip. This differs from
the local-RAG step, which only hints, on purpose: there is nothing to
compile here, just a download.

Accepting it makes setup:

1. download the current stable Chrome for Testing (about 200 MB);
2. download the pinned Nymeria extension release and stage it with its
   optional host permissions rewritten as required, which is what replaces
   every popup click;
3. mint an account token labelled `server-browser` for the bootstrap admin
   and bake it, with the backend URL and a fresh browser id, into the staged
   extension's `config.json`. That token is exempt from the ordinary account
   lifetime and gets its own (`ACCOUNT_SERVER_BROWSER_TOKEN_TTL_DAYS`,
   3650 days), on purpose: it is a machine credential with nothing to renew
   it, and a browser that silently 401s three months after the install is not
   a working default.
   Any earlier token of that label is revoked first, so re-runs do not pile
   up: natively in-process, and on the Docker path through `users issue-token
   --replace`, which is also what the printed manual command uses. Nothing
   else of yours is touched, and without that revoke a handful of re-runs
   would eventually trip the per-account active-token cap (10 by default) and
   the mint would start failing;
4. measure whether Chrome's sandbox works on this host and record the
   answer;
5. label the browser `server browser` and make it your account default;
6. install and start a background service for it.

Failures never fail setup. Each one prints the manual command that would
finish the job. Step 2 is one of them today: no extension release is
published at the pinned tag yet, so a stock run warns there and prints the
`configure` line to finish by hand with `--source` (see `configure` below).

**On the Docker stack the rig is a host process**, not a container: the
wizard runs on the Docker host and points the browser at
`http://localhost:<API_PORT>`. That install runs in two phases, because the
token has to be minted inside the container and the container only exists
once the stack is up. Phase one downloads Chrome; phase two, after the
stack answers its health check, mints the token, stages the extension and
starts the service, then polls `/browser list` for up to 30 seconds for the
extension to subscribe before setting the label and the account default over
`/browser rename` and `/browser default`. A browser that has not connected
inside that window is not made the default; the wizard prints the
`/browser default <id>` to run once it shows up. If you did
not choose "start the stack now", setup prints the three commands instead:
a `docker compose exec ... users issue-token default --label server-browser
--replace`, a `nymeria browser configure`, and a `nymeria browser service
install`. A compose sidecar that would run the rig in-container is
a follow-up, not something this shape does today.

## The `nymeria browser` command

```bash
nymeria browser install     # download Chrome for Testing into the rig home
nymeria browser configure   # stage the extension, bake the config, decide the sandbox
nymeria browser run         # launch in the foreground (the service does this)
nymeria browser stop        # stop this rig's browser
nymeria browser status      # local facts plus the backend's view
nymeria browser service install|uninstall|status|restart
```

Every action takes `--root` (the project root, auto-discovered otherwise)
and `--home` (the rig home, if you keep it somewhere other than the
default).

**`install`** reads Chrome for Testing's last-known-good version feed,
downloads the current stable build for this platform (`linux64`,
`mac-arm64`, `mac-x64`, `win64`), extracts it with its executable bits
restored, and runs `--version` to prove it works. An already-installed
version is a no-op. On a minimal Linux server the binary often cannot start
for want of shared libraries; the failure runs `ldd`, names the missing
sonames, and prints the `apt-get install` line for them. Installing those
needs root, which setup will not take.

Chrome for Testing rather than branded Chrome because branded Chrome
dropped `--load-extension` in v137 and `chrome-headless-shell` never
supported extensions, so it is the only build that can run an MV3 extension
headless.

**`configure`** fetches the pinned extension release, or takes
`--source <dir-or-zip>` for a build you already have. It then rewrites the
manifest, bakes `config.json` (mode 0600 wherever the filesystem has POSIX
modes; the log prints the mode that was actually kept), decides the sandbox,
and writes `rig.json`. The browser id and debug port are stable across
re-configures, so rotating the token or changing the port never
re-identifies the browser to the backend. Useful flags: `--base-url`,
`--token-file` (preferred over `--token`, which lands in shell history),
`--token-stdin`, `--label`, `--debug-port`, `--sandbox auto|on|off`, and
`--adopt-home` (below).

**The release is pinned.** Two constants in `nymeria/server_browser.py`
carry the pin: the release TAG (`v0.29.0`) and the sha256 of that release's
zip as published by the extension repo's tag-driven workflow. A download that
does not match the digest is deleted and refused with both digests named, so
a tampered or partial asset never reaches `ext/`. `--source <dir-or-zip>`
bypasses the fetch (and therefore the pin) for a local build; the extension
guide's release notes describe how the zip is made reproducible so a locally
built zip and the published one carry the same digest. Bumping the extension
means a new tag, then updating both constants from the PUBLISHED asset.

**`configure` stops a running rig and starts it again.** The extension
adopts a changed `config.json` only when its service worker bootstraps, and
the SSE connection keeps that worker alive indefinitely, so re-baking under
a running browser would otherwise be a silent no-op (and rewriting the files
it is running from strands it outright). So `configure` stops the browser
first, and afterwards either restarts its service or, if the rig was running
without one, prints the command that brings it back.
Changing `--debug-port` also retires the old service unit, since a unit is
named for the port: install its replacement with `nymeria browser service
install`.

**`run`** launches the browser in the foreground; a service manager owns
the lifecycle. Plain, on POSIX, it `exec`s into Chrome, so the process you
started becomes the browser. `--supervise` instead runs Chrome as a child
and restarts it on abnormal exit with backoff: the installed service uses
it on every platform, which is why the unit's main process is the
supervisor rather than Chrome, and it is the only mode on Windows, where a
scheduled task supplies no crash supervision of its own. `--fresh-profile`
wipes the profile first (see Upgrading).

A single-instance guard covers both the pidfile and strays: `run` claims the
pidfile exclusively and refuses if a live process already holds it, and
refuses again if any Chrome outside the pidfile is using this profile. That
second check exists because a stale twin once survived a hand-rolled kill
and kept executing every command beside its replacement. Both checks read
the candidate's command line and only count processes that are actually this
rig's browser, so a guard file left behind by a reboot never blocks a start
and never aims a signal at whatever inherited that pid.

**`stop`** asks a supervising `run` to stay down (every installed service
supervises, so killing Chrome alone would just hand the rig back to its own
supervisor), then terminates the pidfile's process plus any stray on the
profile, escalating to SIGKILL after a grace period, and clears the pidfile.
It says so in its output: bring the rig back with `nymeria browser service
restart` or `nymeria browser run`.

**`status`** prints local facts (is Chrome installed, is it configured, is
it running, is the extension's service worker present on the debug port)
and then the backend's own view: `/health`, `/me` with the baked token, and
this browser's row in `/browser list`. Exit code 0 when everything is
healthy, 1 otherwise.

**`service`** installs, removes, queries or restarts the background
service. One unit per rig, named for that rig's debug port (see "Several
instances on one host" for what that does and does not guarantee):

| Platform | Artifact |
|---|---|
| Linux | `~/.config/systemd/user/nymeria-browser-<port>.service` |
| macOS | `~/Library/LaunchAgents/com.nymeria.browser.<port>.plist` |
| Windows | Scheduled task `NymeriaOS Server Browser <port>`, launched through a hidden `.vbs` shim under `~/.nymeria/` |

The systemd unit is a user unit, so on a headless Linux box enable
lingering (`sudo loginctl enable-linger $USER`) or it stops when your login
session ends. macOS LaunchAgents run only inside a logged-in GUI session.

## The rig on disk

The default home is `<root>/data/server-browser`, or whatever
`SERVER_BROWSER_HOME` names. The directory itself is mode 0700.

Precedence: an explicit `--home`, then `SERVER_BROWSER_HOME` (**the process
environment first**, then the root's env files), then the pointer file a
`configure --home` leaves at `<root>/data/server-browser-home` so a later
`nymeria init` reconfigures that rig instead of building a second one, then
the default.

The process-environment step applies only when no `--root` is given: the
launch root is then the target, and a shell export is a deliberate override.
With `--root <B>` the command answers for B alone (its env files, then the
pointer file, then the default), because `nymeria` loads the launch root's
env before anything looks at `--root`, and an env-first lookup there once
made `nymeria browser configure --root <B>`, run from install A's directory,
re-bake A's browser with B's URL and token. `nymeria init` follows the same
root-only rule.

| Path | What it is |
|---|---|
| `cft/<version>/` | One Chrome for Testing install per version; the newest is used |
| `ext/` | The staged extension, with `config.json` (mode 0600) carrying the backend URL, the account token, the browser id, `kind`, and the label |
| `profile/` | The persistent `--user-data-dir`, mode 0700 |
| `rig.json` | The rig's identity and choices: browser id, base URL, label, debug port, sandbox decision and its reason, extension and Chrome versions |
| `chrome.pid` | Single-instance guard |
| `downloads/` | The extension release zip, kept as a cache (a re-`configure` reuses it). The Chrome archive is deleted the moment it is extracted |

**`profile/` is a crown jewel.** Once a human has signed the browser into a
site, the profile holds that live session, and Chrome's Linux profile
encryption is not a meaningful barrier to anyone with the host account.
Treat it like a private key: it is functionally the accounts it holds,
sitting on your server. Four consequences worth knowing:

- `nymeria snapshot` never captures the rig. The default home sits inside
  the data dir the walker covers, so `server-browser` is an explicit
  exclusion beside `voice/` and `logs/`, with no opt-in: a snapshot is the
  one artifact users copy off the box, and the profile and the baked token
  are both things that must not travel in one. The exclusion is both the
  top-level NAME and the rig home resolved exactly the way the launcher
  resolves it, pointer file included, so moving the rig somewhere else
  inside the data dir does not quietly put it back in the artifact.
- **A restore does not restore the rig, and does not remove it either.** A
  restore sweeps every top-level entry of the data dir into
  `.pre-restore-<ts>/` before unpacking the artifact, and the rig is
  deliberately exempt from that sweep, because there is nothing in the
  artifact to put back in its place: sweeping it would move a running
  Chrome's profile out from under it and lose every signed-in session for
  nothing. So expect the rig to survive a restore untouched, and expect the
  accounts database under it to be the artifact's. That last part is what
  bites: the baked token is only still valid if the snapshot was taken after
  that token was minted. Run `nymeria browser status` after a restore, and if
  it reports the token was rejected, `configure --token-file` with a token
  minted from the restored install. A restore onto a FRESH host has no rig at
  all: build one with `nymeria browser install` and `configure`, and sign it
  back in. The pointer file gets the same treatment for the same reason: it
  is host-local, so it is neither captured nor swept aside, and a rig you
  configured with `--home` is still the one your restored install finds.

- The debug port is bound to loopback and must stay there. CDP has no
  authentication of any kind: anyone who reaches that port can read every
  cookie, navigate anywhere, and run script as any origin. Never pass
  `--remote-debugging-address=0.0.0.0`, and remember that Docker-published
  ports bypass a host firewall.
- Loopback is a HOST boundary, not a user boundary. Because the port has no
  authentication, every local account on that machine can attach to it and
  read the rig's cookies and sessions, whatever the 0700 on `profile/` says:
  the file mode protects the profile at rest, and nothing protects it from a
  local user while the browser is running. Treat a shared-login box as a
  place where the rig's accounts are shared too.

## Signing it in

The server browser starts signed into nothing. A login wall there is not a
fault, it is the state until a human signs it in, after which that site
stays signed in in the profile.

Two facts shape how that works, both measured against Chrome for Testing
152 on 2026-08-28 (Google tunes sign-in risk logic server-side without
notice, so re-measure before relying on them):

- **The login must happen inside this browser.** Copying cookies from your
  desktop Chrome does not survive: Device Bound Session Credentials tie a
  Google session to a hardware key on the machine that made it, with no
  toggle to disable, and the whole industry independently converged on
  "sign in on the machine you will run from".
- **This browser can be signed in.** It presents a headful User-Agent
  derived from the installed binary's version, and never passes
  `--enable-automation`. Those were the two independent hard blocks; with
  both handled, the normal sign-in flow is served.

**Passkeys are unavailable** in this browser: no platform authenticator
exists, so `isUserVerifyingPlatformAuthenticatorAvailable()` is false. Have
a fallback ready (TOTP, backup codes, or a prompt on your phone) before you
start. And expect a first-login device-verification challenge, since the
browser is on a datacenter IP the site has not seen you from before.

### The desktop login handoff (the only supported route)

`chrome_request_login` (the agent) and `/browser login <url>` (you) start a
handoff: one tab is screencast into Nymeria Desktop, you drive it with your
own keyboard and mouse, and the agent is locked out of that tab entirely
(it cannot drive it and cannot see a pixel) until the session ends. Your
password never enters the agent's context. One session per user at a time,
with a hard ten-minute cap.

The viewer lives in Nymeria Desktop and nowhere else: not the CLI, not a
chat app, not the phone (the mobile client has the API client but no UI
yet). Nothing tells the backend whether a viewer is actually attached, so a
handoff started with Desktop closed simply runs out its ten minutes with
nobody watching. Open Desktop first.

### There is no second route, by design

Signing in through the hosted DevTools frontend over an SSH tunnel used to
be documented here as a fallback. It is gone: the rig now launches with
`--remote-allow-origins=devtools://devtools` and nothing else, so the
debugger WebSocket rejects a connection carrying any other `Origin`,
including the public `chrome-devtools-frontend.appspot.com` the tunnel
recipe relied on.

That is a deliberate trade. The origin allowlist and the loopback bind are
separate controls: the bind stops other hosts reaching the port, while the
origin check stops web content in a browser on THIS host attaching to a
browser that holds live signed-in sessions. A page served from an allowed
origin needs only a target id to get full CDP over it, so "it is
loopback-bound anyway" is not an argument for listing a public web origin.
Never widen the list, and never to `*`.

So: open Nymeria Desktop and use the handoff. If Desktop is genuinely out of
reach, the honest options are to sign the site in later when it is not, or
to run the rig somewhere you can point Desktop at.

### Sessions still go stale

Every vendor's documentation warns that persisted sessions expire, and none
can prevent it. Expect to repeat a handoff for a site occasionally; the
agent will hit the login wall and ask.

## The Linux sandbox

Stock Ubuntu 23.10 and later restrict unprivileged user namespaces through
AppArmor, and Chrome aborts at launch when it cannot build its sandbox.

Nymeria **measures** rather than infers this, because inference is
unreliable here: Ubuntu ships an AppArmor profile for `unshare` itself, so
a user-namespace probe passes while Chrome still fails. `configure`
launches Chrome once with the sandbox and, only if that fails, once more
with `--no-sandbox`, then persists the answer in `rig.json`. If neither
works it refuses and reports why.

When the fallback is in force it is never silent: `configure` warns,
every `run` logs it, `nymeria browser status` shows `Sandbox: OFF` with
the measured reason, and `nymeria doctor` keeps warning for as long as the
rig runs that way.

The durable fix is a root-installed AppArmor profile for the Chrome binary,
which is a distribution-level change Nymeria will not make for you. Once
one is in place, re-enable the sandbox explicitly:

```bash
nymeria browser configure --sandbox on --token-file <file>
nymeria browser service restart
```

`--sandbox off` forces the opt-out without measuring, and `--sandbox auto`
(the default) measures.

## Windows and macOS

Both are supported by the same commands. Chrome for Testing ships `win64`,
`mac-arm64` and `mac-x64` builds, and `configure`, `run`, `status` and
`service` all work there.

- **Windows** uses a scheduled task registered through `schtasks`, running
  a hidden `.vbs` shim so no console window flashes, started at logon with
  limited privileges. A scheduled task supplies no crash supervision, which
  is what `--supervise` is for. The sandbox measurement still runs (it is a
  real Chrome launch), it just always concludes the sandbox works: the
  AppArmor problem is Linux-only. The 0600 and 0700 modes this page names
  are POSIX, so Windows keeps the rig's files at whatever the parent
  directory's ACL gives them; `configure` logs the mode it actually got
  rather than claiming one.
- **macOS** uses a LaunchAgent, which runs only while a user is logged into
  the GUI. A Mac that sits at the login window does not run the rig.
  Extraction goes through `ditto` (Apple's own, else `unzip`) because the
  browser ships as an `.app` whose framework is reached through symlinks,
  and Python's own zip extractor turns those into plain files, leaving an
  app that cannot start. If neither tool is present, `install` says so
  instead of laying down a broken browser.

Neither the Windows nor the macOS path has been verified on real hardware;
treat both as supported but unproven.

## Several instances on one host

Each Nymeria instance gets its own rig, and what keys it apart is the DEBUG
port, not the API port:

- the home defaults to that instance's `<root>/data/server-browser`, so the
  profiles never mix;
- the debug port is chosen free at `configure` time (from 9222 upward) and
  persisted in `rig.json`;
- the service unit is named for that debug port
  (`nymeria-browser-<port>.service`, `com.nymeria.browser.<port>`,
  `NymeriaOS Server Browser <port>`), which is why a `configure --debug-port`
  retires the unit it just renamed out from under.

The free-port pick only sees ports that are LISTENING at the moment you run
`configure`, so two rigs configured while both browsers are stopped both
choose 9222 and then fight over it at launch. Configuring the second rig
while the first is running avoids that; so does passing `--debug-port`
yourself, which is the reliable way to lay out a multi-instance host.

Run each command with `--root <that instance's root>` and it finds the
right rig on its own; only a bare command (no `--root`) reads
`SERVER_BROWSER_HOME` from the environment you are running from (see "The
rig on disk").

## Upgrading

Chrome for Testing: `nymeria browser install` fetches the current stable
build into a new `cft/<version>/` directory and the newest installed
version is used from then on. Old versions are left in place; delete them
by hand if you want the disk back. The User-Agent is derived per launch
from the binary, so an upgrade cannot leave a version-mismatched UA
behind, which would itself be a sign-in signal.

The extension: bump the pinned release and re-run `configure`, or point
`configure --source` at a build.

**A re-configure only takes effect across a browser restart**, and
`configure` performs that restart itself (the mechanism, and the one case
where it cannot, are under `configure` above). Read its output: if it says
the browser was stopped and not started again, run the command it printed.

```bash
nymeria browser configure --root <root> --token-file <file>
nymeria browser status --root <root>    # confirm: connected, new identity
```

**Chrome caches the extension's worker script inside the profile**
(`Default/Service Worker/ScriptCache`, keyed by the extension's origin, which
the manifest key pins), and a plain restart keeps EXECUTING the cached build
while announcing the new manifest version. Measured on this rig: an adopted
v0.28.0 profile ran the old worker (old identity, old token, no kind or
label) through two full Chrome restarts and never adopted the new bake.
`configure` therefore clears that cache on every run, with the rig stopped,
so an upgrade or an adoption runs the staged build on its first start; the
sessions beside the cache are untouched. A build id carried by the script
itself, so a QA round can prove which code is running without trusting the
manifest, is still backlog #285. The fresh-profile recipe below remains the
last-resort reset for anything else the profile may be holding onto:

```bash
systemctl --user stop nymeria-browser-<port>       # Linux; launchctl / schtasks elsewhere
nymeria browser run --fresh-profile --root <root>  # foreground; see below
systemctl --user start nymeria-browser-<port>
```

That middle command is not a supervisor you hand the rig off to. On POSIX a
plain `run` **execs** into Chrome, so the process sitting in your terminal
IS the browser: check from a second shell that `nymeria browser status`
reports it connected, then Ctrl-C, which ends Chrome itself, and start the
service again.

That **destroys every signed-in session in the profile**, so it is a last
resort, not routine hygiene. The baked config re-adopts on the first run
afterwards. Note that `chrome_reload_extension` is NOT the way out of this:
tried against a headless rig, it stranded the extension and needed a manual
restart.

Related and still open: the extension is not in the Chrome Web Store
(backlog #283), so the release zip plus load-unpacked is the beta path for
your own Chrome. The server browser is unaffected by the developer-mode nag
that path carries, because it has no UI to show it in.

## Adopting an existing rig

If you already run a headless Chrome with this extension by hand, point
`configure` at its home and its signed-in sessions come with it:

```bash
nymeria browser configure --adopt-home ~/.nymeria-browser --token-file <file>
```

The old profile is COPIED, not moved, and the original is left alone.
Adoption refuses if the new home already has a profile: remove it first, or
drop the flag. The copy's cached service-worker script is dropped (see
Upgrading), otherwise Chrome would keep running the OLD extension build out
of the copy and the new bake would never be adopted. A hand-run rig whose
bake predates this feature announces itself as kind `desktop` until it is
re-baked, which is expected. `configure` provisions and connects the rig; it
does not make it the account's default browser (the wizard does that on its
own path). If the account also has another browser connected, every
`chrome_*` call refuses until a target exists, so finish with `/browser
default <browser id>` from any chat surface (the id is in `nymeria browser
status`).

## Uninstalling

```bash
nymeria browser service uninstall --root <root>   # stop and remove the unit
nymeria browser stop --root <root>                # if it is still running
rm -rf <root>/data/server-browser                 # the rig, profile included
```

Then clear `SERVER_BROWSER_HOME`, or the `chrome_*` refusals keep telling
you this install has a server browser. A `nymeria init --non-interactive
--no-server-browser` run AFTER the rig is gone retires the key for you
(setup only keeps it while a rig exists); otherwise delete the line from
your env file by hand. Revoke the
token too if you are not reinstalling: it is the account token labelled
`server-browser` in Desktop > Account > Tokens.

Declining the wizard step on a reconfigure does NOT remove an existing rig:
setup says so and leaves it alone.

## Troubleshooting

Start with `nymeria browser status` on the host. It answers in three
layers, and the first failing layer is the one to fix.

| What it prints | What it means |
|---|---|
| `Chrome for Testing is not installed` | Run `nymeria browser install` |
| `Not configured` | Run `nymeria browser configure` |
| `Not running` | Start the service: `nymeria browser service restart` |
| `pid N is alive but the debug port did not answer` | Chrome is up but wedged, or a second rig took the port. `nymeria browser stop` then restart the service |
| `Extension worker: absent` | Normal on its own: an idle MV3 worker is stopped by Chrome. The backend view below it is the truth |
| `Backend not reachable at <url>` | The API is down, or the baked base URL is wrong. Re-`configure` with the right `--base-url`, then restart |
| `The baked token was rejected` | The token was revoked or expired. Mint a new one and re-`configure`, then restart |
| `Backend view: this browser is known but NOT connected` | The worker is probably mid-reconnect; retry in a minute. If it persists, restart the service |
| `Backend view: this browser is not in the roster yet` | It has never subscribed since the backend started. Restart the service and re-check |
| `Sandbox: OFF` | The measured fallback (see the sandbox section); not a failure |

`nymeria doctor` carries a `Server browser` row whenever this install has
one (it is absent entirely on an install that never configured a rig). It
**warns, never fails**: the backend runs fine without a browser. A healthy
rig reports the Chrome version, the identity it connected as, its label, its
kind and its debug port; a rig on `--no-sandbox` warns with the AppArmor fix
named; anything else repeats `status`'s problems and points at it. One
branch is not the rig's fault and says so: when the backend itself is not
up (as during `nymeria init --doctor`, which runs before start-now), the row
reports what is locally true and calls the connection unknown.

From the agent's side, `chrome_health()` with no tab id is a connection
probe: it lists every browser known to this backend process with its kind,
connected state and version, and its not-connected notes are aware of both
the kind and the install. If the tools refuse with "no browser is
connected", that refusal already names the right fix.

Two more places to look:

- **The service's own log.** `journalctl --user -u nymeria-browser-<port>`
  on Linux; `<root>/logs/server-browser-<port>-stdout.log` and
  `-stderr.log` on macOS; `schtasks /Query /TN "NymeriaOS Server Browser
  <port>" /V /FO LIST` on Windows.
- **A restarted API forgets its roster.** The registry is in-process, so
  right after a backend restart a probe honestly reads "unknown" until the
  extension resubscribes. Give it a minute before concluding anything.

## Related

- [remote-access.md](deployment-remote-access.md): the browser extension's
  HTTPS-or-localhost requirement, which the server browser sidesteps by
  living on the same host as the backend.
- [backup-and-restore.md](backup-and-restore.md): what `nymeria snapshot`
  captures, including the caveat above.
- [../agent-systems/tools.md](../agent-systems/tools.md), "Chrome Extension Tools": the tool
  surface, the routing ladder and the roster.
- [../api.md](../api.md): `client_kind` and `client_label` on the extension's
  stream connect.
