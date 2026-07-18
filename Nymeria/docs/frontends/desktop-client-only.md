# Desktop Thin-Client Mode

The Tauri desktop app supports two runtime modes:

- **Thin-client** (default): no local backend is launched; the frontend connects to a remote backend (e.g. a VPS deployment).
- **Self-contained**: Tauri spawns a local Python backend from a source checkout (`python run.py api`/`worker`) and waits for it to come up on `127.0.0.1:8000`. The frontend talks to the local API.

This doc covers thin-client mode (the default) and how to opt into self-contained mode for source-checkout dev work.

## Picking the mode

`nymeria-desktop/src-tauri/src/lib.rs` decides at startup. The order of checks:

1. Default → **thin-client**.
2. If `NYMERIA_SPAWN_BACKEND=1` (or `true`/`TRUE`) is set AND a source checkout is detected (the running `.exe` has an ancestor containing both `Nymeria/` and `nymeria-desktop/`) → **self-contained**.
3. If `NYMERIA_CLIENT_ONLY=1` is set, it forces **thin-client** even when `NYMERIA_SPAWN_BACKEND=1` is also set. The flag is redundant with the new default but is still honored so existing launch scripts continue to work.

Self-contained mode is now opt-in. Running `npm run tauri dev` with no env vars will start in thin-client mode and surface no startup errors even if `Nymeria/run.py` is unreachable from the desktop process.

## Why this used to silently freeze the UI

When a backend spawn failed in self-contained mode, the Rust side emitted a `backend-status: failed:<msg>` Tauri event. The JS listener in `nymeria-desktop/src/lib/stores/backendProcess.svelte.ts` registered for that event via a dynamic `import('@tauri-apps/api/event')`, which is async. If the Rust failure landed *before* the JS listener attached, the event was dropped and `status` stayed at `'starting'`, leaving the user on `StartupOverlay.svelte`'s spinner forever (black screen, no error message).

This was fixed by:

- Caching the most recent `backend-status` payload in `AppState.lifecycle_status` (a `Mutex<String>`) on the Rust side.
- Exposing `get_backend_lifecycle_status` as a Tauri command.
- Having the JS listener query that command immediately after attaching, so any missed event is replayed.

The race fix is independent of thin-client mode and helps any future startup failure surface as the proper error overlay rather than a silent hang.

## Launching in thin-client mode

Thin-client is the default, so no env vars are required:

```powershell
cd nymeria-desktop
npm run tauri dev
```

```bash
cd nymeria-desktop && npm run tauri dev
```

After the desktop app launches, point its API URL at your remote backend in the in-app settings (or the SetupWizard on first run). For a VPS-hosted instance: `https://your-vps-domain/`. On first-run completion the wizard exchanges the pasted token for a long-lived personal token (so a pasted 24h bootstrap token survives), and warns when the app and backend versions differ, which with no auto-update means fetching the matching installer.

## Opting into self-contained (source-checkout) mode

Set `NYMERIA_SPAWN_BACKEND=1` before launching. Tauri will detect the source checkout, write/refresh the per-machine API key in `Nymeria/.env` via `auto_config::ensure_env_file`, and spawn the API and worker from `Nymeria/run.py`.

```powershell
$env:NYMERIA_SPAWN_BACKEND = "1"
cd nymeria-desktop
npm run tauri dev
```

```bash
NYMERIA_SPAWN_BACKEND=1 npm run tauri dev
```

## Verifying you're in thin-client mode

- The `StartupOverlay` should flicker briefly (or not appear at all). Rust emits `ready` immediately when there's no `ProcessManager`.
- The settings panel will show no local backend process status (since there isn't one).
- `commands::get_auto_config` returns `Err("Client-only mode — configure backend URL in settings")`, so the SetupWizard / SettingsPanel won't try to auto-fill `localhost:8000`.

## When NOT to use thin-client mode

If you want Tauri to manage a local Python backend for you (the source-checkout dev workflow), set `NYMERIA_SPAWN_BACKEND=1` as shown above. Self-contained mode requires a working source checkout (`Nymeria/run.py` reachable, Python deps installed) and auto-creates a per-machine API key in `Nymeria/.env` via `auto_config::ensure_env_file`, which thin-client mode skips.

## Code references

- `nymeria-desktop/src-tauri/src/lib.rs`: `env_flag`, `emit_status`, `run()` mode selection
- `nymeria-desktop/src-tauri/src/commands.rs`: `get_backend_lifecycle_status`
- `nymeria-desktop/src-tauri/src/process_manager.rs`: `detect_project_root`
- `nymeria-desktop/src/lib/stores/backendProcess.svelte.ts`: listener + replay
- `nymeria-desktop/src/lib/components/common/StartupOverlay.svelte`: UI shown while `status !== 'ready'`
