# Web Client

The backend serves a full web client at its own origin: open
`http://localhost:8000` (or your deployment's URL) and you are in the same
app the desktop build ships. For the beta, this is the primary GUI: no
install, no signing, works on any machine with a browser, including phones.

## What it is

The web client is the desktop app's SvelteKit build (adapter-static with an
`index.html` SPA fallback), bundled into the Python package at
`nymeria/frontend/` and served by the API process in both deployment shapes:

- `/_app/*` and root assets (favicon, brand mark) are served statically.
- Browser routes fall back to `index.html` (the SPA router takes over);
  API-shaped misses still 404 rather than swallowing errors.
- Concrete API routes always win over frontend paths.
- Every response carries a Content-Security-Policy whose `script-src` has no
  `'unsafe-inline'`: the bundle's own inline scripts are admitted by sha256
  hash, computed at startup from the `index.html` actually served, with CR
  and CRLF folded to LF first. The browser hashes the parsed script text, not
  the file bytes, so a bundle built from a CRLF checkout would otherwise have
  every inline script (theme and preference pre-boot included) silently
  blocked while the page still rendered.

Because it is the same build, features arrive in the web client and the
desktop app together. The differences are only where the desktop shell adds
OS integration (see below).

## First run

The happy path needs no token at all: a fresh interactive `nymeria init`
that starts the backend opens your browser already signed in via a one-time
`#token=` URL fragment (consumed once, scrubbed from the address bar,
exchanged for a long-lived personal token). See the "First-run browser
handoff" section in [agent-systems/accounts.md](../agent-systems/accounts.md).

On every other path (Docker, headless installs, a second device or
browser): open the backend URL. The client probes the serving origin's
`/health`, and because a page served by a Nymeria backend is standing in
front of a deployment that already exists, it opens on a token-only sign-in
(one field, Verify shows the account name and role, Sign in) instead of the
full setup hub. Paste an account token (the bootstrap token from
`<data_dir>/BOOTSTRAP_TOKEN.txt` on a first boot, or a token minted with
`run.py users`), and you are in. "Open full setup" on that screen reaches the
hub (backend URL field, provider, RAG and integration sections) for a fresh
install or for pointing the app at a different server; the Tauri desktop app
and a browser build not served by a backend open on the hub directly.
Routing lives in `src/lib/utils/firstRun.ts`.

## What is desktop-only

Everything else works from the browser, including admin settings, thread
config, tools and MCP management, workflows, hooks, dashboards, and (for
admins) CLIProxy subscription management, which is backend-first REST and
fully usable from the web client. Only the OS-integration conveniences are
desktop-shell-only and hide themselves in a browser:

- Local backend process management (start/stop/status of a shell-managed
  backend, the startup splash).
- The CLIProxy local-sidecar container start/stop buttons (the management
  panel itself works everywhere).
- OS keychain token storage, the system tray, and native auto-config.

If you prefer the installed desktop app (currently Windows), grab the
installer from the project's release page; it is the same UI as a thin
client over the identical backend URL + token flow.

## Token storage caveat

In a browser, your account token is stored in the browser's localStorage
(keys prefixed `secfallback:`), not an OS keychain: localStorage is
readable by any script running on the page, so it is one notch weaker than
the desktop shell's keychain. For the beta's supported topologies
(localhost, a private tailnet, or an HTTPS tunnel you control) this is an
accepted tradeoff; do not expose the backend origin to hostile pages, and
prefer the desktop app if keychain-backed storage matters to you. Signing
out clears the stored token.

## Remote access

To reach the web client from other devices, put the backend behind one of
the supported remote-access paths (Tailscale Serve, a Cloudflare tunnel, or
your own reverse proxy with HTTPS, e.g. Caddy). The init wizard can guide
Tailscale and Cloudflare end to end. Details and tradeoffs:
[deployment/deployment-remote-access.md](../deployment/deployment-remote-access.md).
A phone browser against a tailnet or tunnel URL is the supported interim
mobile client for the beta.

## Rebuilding the UI from source

Package installs ship the built frontend; source installs can refresh it
after frontend changes:

```bash
./scripts/build_frontend.sh   # repo root; builds and bundles, then restart the backend
```

The script mirrors the release pipeline's exact build-and-bundle steps
(`npm install && npm run build` in `nymeria-desktop/`, then a copy into
`Nymeria/nymeria/frontend/`). This applies to the Docker source stack too:
the API serves the package copy `nymeria/frontend/` (bind-mounted with the
rest of the Python source) whenever it holds a build, and the
`nymeria-desktop/build` mount is only a fallback for when it does not. So
run the script on the host, then `docker compose restart api`; no image
rebuild is needed.
