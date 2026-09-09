# Backup and Restore (User-Data Snapshots)

Nymeria ships a first-class disaster-recovery path: the `snapshot` CLI
captures everything a deployment cannot regenerate into one encrypted,
self-contained artifact, verifies it, and restores it onto the same or a
fresh host.

```bash
python run.py snapshot create     # capture (stack may keep running)
python run.py snapshot verify <artifact>
python run.py snapshot list
python run.py snapshot restore <artifact>   # offline only
```

## What a snapshot contains

| Source | Contents | Consistency |
|--------|----------|-------------|
| Data dir | Every store: `accounts.db` (accounts, tokens, credential vault, chat bindings), memory indexes, TODO schedule, skills and tool indexes, all JSON stores (todos, triggers, hooks, workflows, MCP servers, custom tools, thread configs, profiles), prompt overrides, auth token caches | SQLite databases are copied with the online backup API (transactionally consistent even against live writers); JSON stores are captured per-inode (always a complete old-or-new version) |
| Postgres (Docker shape) | The four LangGraph checkpoint tables (conversation history) | One `REPEATABLE READ` transaction, so the tables are consistent with each other; no `pg_dump` binary required |
| Workspace | `threads/` (attachments) and `images/` (prompt and generated images, which survive compaction) | Per-file capture; agent scratch is excluded |
| Vault key | `NYMERIA_SECRETS_KEY`, embedded inside the encrypted envelope | Makes the artifact self-contained: one file + one passphrase restores the credential vault on a bare host |

Not captured: `data/snapshots/` (the output dir), `data/logs/`, `data/flags/`,
`data/voice/` (regenerable cache), `data/server-browser/` and
`data/server-browser-home` (the server browser's rig and the pointer to a
relocated one; see below), and `data/backups/` (self-modification
source rollbacks; add with `--include-code-backups`). Redis holds nothing
durable (pure pub/sub) and is skipped entirely.

The server browser is excluded on purpose and cannot be opted back in: its
`ext/config.json` carries a live account token and its `profile/` carries
every site session a human signed the browser into, neither of which belongs
in an artifact you are encouraged to copy off the box. Its `cft/` is a
few hundred MB of re-downloadable Chrome besides. The exclusion covers both
the name `server-browser` and the rig home resolved exactly the way
`nymeria browser` resolves it (`SERVER_BROWSER_HOME`, the pointer file a
`configure --home` leaves, the default), so moving the rig elsewhere inside
the data dir does not put it back in the artifact. That pointer file,
`data/server-browser-home`, is left out too: it holds no secret, but it is an
absolute path that is only true on the host that wrote it.

The restore direction follows from that, and is the half worth stating: an
in-place restore leaves an existing rig alone. Restore sweeps every
top-level entry of the data dir into `.pre-restore-<ts>/` before unpacking,
and the rig and its pointer file are exempt from that sweep, because the
artifact has nothing to put back in their place: sweeping the rig would move
a running Chrome's profile out from under it and lose every signed-in
session for nothing, and sweeping the pointer would lose the only record of
where a hand-configured rig lives. What DOES change
under it is the accounts database, so the browser's baked token is only
still valid if the snapshot was taken after that token was minted. Run
`nymeria browser status` afterwards and, if the token was rejected,
re-`configure` with one minted from the restored install. On a fresh host
there is no rig at all: build one with `nymeria browser install` and
`nymeria browser configure`, and sign it back in.
Details: [server-browser.md](server-browser.md).

Consistency model: every individual store is internally consistent; across
stores the capture window is a few seconds while the stack keeps running,
which matches what the app already tolerates at startup. For a perfect
point-in-time image, stop the stack first (cold snapshot):

```bash
docker compose --env-file .env.docker stop api worker mcp
docker compose --env-file .env.docker run --rm api python run.py snapshot create
docker compose --env-file .env.docker start api worker mcp
```

## The artifact and its passphrase

`snapshot create` writes `snapshot-<utc>.nysnap` into
`data/snapshots/` (override with `NYMERIA_SNAPSHOTS_DIR` or `--output`). The
artifact is a tar.gz inside a passphrase-encrypted envelope (scrypt key
derivation, chunked AES-256-GCM, streaming, tamper- and truncation-detecting).

The passphrase is resolved from, in order: `--passphrase-file`, the
`NYMERIA_SNAPSHOT_PASSPHRASE` env var, or an interactive prompt. Because the
artifact embeds the Fernet vault key, **the passphrase is the one secret you
must keep outside the host**, and the artifact is safe to ship anywhere.

`--no-encrypt` writes a plain tar.gz for pipelines that encrypt downstream; it
deliberately refuses to embed the vault key, so you must preserve
`NYMERIA_SECRETS_KEY` separately or lose the credential vault.

After every create, the artifact is automatically re-read and verified
(`--no-verify` skips this). Verification checks member digests against the
manifest, runs `PRAGMA integrity_check` on every SQLite copy, parses every
JSON store, validates the Postgres dumps, and runs the **vault canary**: it
decrypts one real credential ciphertext with the embedded key, proving the
key and the data actually belong together.

## Getting artifacts off the host

Shipping the artifact off-host is deliberately your job (cron plus
scp/rclone/restic to wherever you trust). Snapshots that stay on the same
disk protect against bad migrations and operator mistakes, not disk loss.

```bash
# Docker shape: create inside the api container, then copy out
docker exec nymeria-api python run.py snapshot create
docker cp nymeria-api:/data/snapshots/. ./snapshots-out/
scp ./snapshots-out/*.nysnap backup-host:nymeria-snapshots/
```

Bare-metal slim: run `python3 run.py snapshot create` directly; artifacts land
in `<data_dir>/snapshots/`.

## Restore

Restore is offline-only: it refuses while the stack looks live (API health on
localhost and on the compose-internal `nymeria-api` hostname, plus service
heartbeats) unless `--force`. `--force` overrides the liveness guard ONLY;
verification always gates the restore. It never deletes: the current data dir
contents are moved into `.pre-restore-<utc>/` inside the data dir.

Docker shape (Postgres must be up, everything else down):

```bash
cd Nymeria
docker compose --env-file .env.docker stop api worker mcp
docker compose --env-file .env.docker run --rm api \
  python run.py snapshot restore /data/snapshots/<artifact>.nysnap
docker compose --env-file .env.docker start api worker mcp
```

Slim / bare metal: stop the service (`nymeria service uninstall` or Ctrl+C),
run `python3 run.py snapshot restore <artifact>`, start again.

Restore order: Postgres checkpoints first (one transaction; the target ends
fully restored or untouched, and the LangGraph schema migration version must
match the snapshot's), then the data dir content swap (pure renames), then
workspace, then vault-key reconciliation:

- env key matches the artifact: nothing to do.
- env key missing: the key is printed with instructions, or merged into an
  env file with `--write-env <path>`.
- env key DIFFERENT: loud warning; the restored vault needs the artifact's
  key, and adopting it orphans anything encrypted with the current one.

Cross-backend restore (a Postgres-shape snapshot into a slim/SQLite
deployment or vice versa) is not supported; the manifest records the backend
and restore refuses on mismatch.

The rename step covers every top-level entry the artifact could replace. The
server browser's rig is the one exemption (above): it is never captured, so
moving it aside would destroy live state rather than swap it. The other
uncaptured entries (`snapshots/`, `logs/`, `flags/`, `voice/`) ARE swept
aside, so your other artifacts end up under `.pre-restore-<ts>/snapshots/`
rather than gone. Look there before concluding anything was lost.

After a restore, start the stack and run `nymeria doctor`.

## Non-interactive use

Every action works headless: `--passphrase-file` or
`NYMERIA_SNAPSHOT_PASSPHRASE` for the passphrase, `--yes` to skip the restore
confirmation, `--force` to override stale liveness signals. Exit codes: 0
success, 1 failure (including verification failures), 2 usage errors.
