# Secrets Management — git-crypt

As of commit `936cd89`, secret files live **inside** the repo, encrypted at rest with [git-crypt](https://github.com/AGWA/git-crypt).

## Policy

git-crypt is the accepted secrets strategy for this project. Rationale:

- **Single-developer, private repo.** One person holds the symmetric key; there is no multi-user key distribution problem.
- **Encrypted at rest in GitHub.** Pushed ciphertext is opaque without the key file, so a compromised GitHub token or accidental repo visibility change does not leak secrets.
- **Transparent in the working tree.** After `git-crypt unlock`, encrypted files behave like normal files — no extra tooling for edits, diffs, or Docker bind-mounts.
- **Fallback without the key.** `Nymeria/.env.docker.example` lets a fresh clone start the stack by filling in new keys, so the unlock key is not a hard blocker.

### Per-machine drift is expected

Each machine (VPS, work PC, personal PC) may have different values in `.env.docker` — different CLIProxy URLs, different API keys, different Postgres passwords. This means `git status` will often show `.env.docker` as modified. That is normal and intentional. Only commit `.env.docker` changes when you want the committed baseline to change for all machines (e.g., adding a new variable with a sensible default). When pulling on another machine after such a commit, resolve the merge with that machine's values.

### Docker build context

`Nymeria/.env.docker` is intentionally excluded from the Docker build context
by `Nymeria/.dockerignore`. Docker Compose still passes it with
`--env-file .env.docker` and bind-mounts it into containers at runtime, but
image layers must not contain plaintext working-tree secrets after
`git-crypt unlock`.

### When NOT to use git-crypt

Move a secret to a per-machine gitignored file when any of these apply:

- The value is ephemeral and rotates frequently (e.g., short-lived OAuth access tokens — these already live in CLIProxy's gitignored `temp/latest/auths/` directory).
- The value is machine-specific with no shared baseline (e.g., a one-off test API key).
- A second developer or CI runner would need independent secrets without sharing the symmetric key.

If the project gains additional developers, re-evaluate: switch to a `.env.docker.local` gitignored override layer or a secrets manager, and move per-machine values out of git-crypt.

## Encrypted Files

Defined in `.gitattributes`:

- `Nymeria/.env.docker` — Docker deployment secrets
- `Nymeria/.env` — Local dev secrets (when present)
- `Nymeria/firebase-service-account.json` — FCM push credentials
- `Nymeria/google_credentials.json` — Google OAuth credentials

## Intentionally tracked client config

`nymeria-watch/app/google-services.json` is not encrypted with git-crypt on
purpose. It is Firebase's Android client configuration for the Wear OS
companion app, not the FCM server credential. The file may contain Firebase
project identifiers, the Android app ID, storage bucket name, and the
Firebase-provisioned Android API key in `current_key`; those values are shipped
inside the Android client build and are not treated as server secrets when the
key is restricted to Firebase services.

Keep the server-side FCM service account in
`Nymeria/firebase-service-account.json`, and keep that file under git-crypt.
If the watch app ever needs a non-Firebase Google API, create a separate
restricted API key for that API instead of reusing or expanding the Firebase
client key.

## Fresh-clone setup

1. Install git-crypt:
   - Linux: `sudo apt install git-crypt`
   - macOS: `brew install git-crypt`
   - **Windows: `scoop install git-crypt`** — winget does NOT have it. If scoop isn't installed, one-liner from PowerShell: `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force; irm get.scoop.sh | iex`
2. Clone the repo as usual.
3. Unlock with the symmetric key (transferred out-of-band via password manager / USB / secure DM — **the key is never in the repo**):
   ```bash
   git-crypt unlock /path/to/nymeria-gitcrypt.key
   ```

## How it works

After unlock, the encrypted files are transparent in the working tree — read/edit/diff them like any other file. Commits automatically re-encrypt via git's clean filter, so you cannot accidentally leak plaintext by committing.

`git-crypt status` shows which files are managed by git-crypt (it labels them "encrypted" because that refers to the in-repo storage format — the working tree is plaintext after unlock).

Other gitignored files (build outputs, lock files, IDE state, log files) are still in `.gitignore` as normal — git-crypt only handles the entries listed in `.gitattributes` with `filter=git-crypt`.

## Without the key

You can still bring up a fresh stack by copying `Nymeria/.env.docker.example` to `Nymeria/.env.docker` and filling in your own API keys, but you'll be missing the firebase/google JSON files (FCM and Google integrations will be inactive — that's fine for most local dev).

## Secret rotation checklist

When a secret is compromised or needs routine rotation, follow the steps for the affected category.

### LLM API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `EMBEDDING_API_KEY`)

1. Generate a new key in the provider's dashboard.
2. Update `Nymeria/.env.docker` on each machine.
3. Restart the API and worker containers: `docker compose --env-file .env.docker restart api worker mcp`
4. Revoke the old key in the provider's dashboard.
5. Verify: `curl http://localhost:8000/health` and send a test message.

### CLIProxy OAuth tokens (Claude/Codex)

CLIProxy OAuth tokens live in `CLIProxyAPI-main/temp/latest/auths/` (gitignored, per-machine). They are not in git-crypt. To rotate:

1. Re-run the OAuth login flow (see the global CLAUDE.md for the `--claude-login` steps).
2. Patch `tool_prefix_disabled: true` into the new auth JSON.
3. `docker restart cli-proxy-api-latest`
4. Verify: `python3 Nymeria/tools/check_cliproxy_cloak.py`

### Postgres password (`POSTGRES_PASSWORD`)

1. Update `POSTGRES_PASSWORD` in `Nymeria/.env.docker`.
2. Update the password inside the running Postgres container:
   ```bash
   docker exec nymeria-postgres psql -U nymeria -c "ALTER USER nymeria WITH PASSWORD 'new-password';"
   ```
3. Restart all backend containers: `docker compose --env-file .env.docker restart`
4. Verify: `docker exec nymeria-postgres psql -U nymeria -d nymeria -c "SELECT 1;"`

### `NYMERIA_SERVICE_TOKEN`

1. Create a new admin token: `docker exec nymeria-api python run.py users add bot-service-new@localhost --role admin`
2. Update `NYMERIA_SERVICE_TOKEN` in `Nymeria/.env.docker`.
3. Restart all thin-client containers: `docker compose --env-file .env.docker restart worker mcp watchdog`
4. Restart any active bot containers (telegram, discord, twitch).
5. Delete or deactivate the old service user if desired.

### `NYMERIA_SECRETS_KEY` (Fernet encryption key for BYO bot tokens)

This key encrypts user-supplied secrets stored in `accounts.db`. Rotation requires re-encrypting all stored ciphertexts — there is no automated tool for this yet. If the key is compromised:

1. Generate a new key: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Decrypt all stored secrets with the old key, re-encrypt with the new key (manual DB operation).
3. Update `NYMERIA_SECRETS_KEY` in `.env.docker` and restart.
4. Ask affected users to re-register their BYO bots if re-encryption is not feasible.

### Firebase and Google credentials (JSON files)

1. Rotate the credential in the Google Cloud / Firebase console.
2. Download the new JSON and replace the file at the same path.
3. `git add` and commit (git-crypt re-encrypts automatically).
4. Restart containers that use the credential.

### git-crypt symmetric key itself

The symmetric key (`nymeria-gitcrypt.key`) is not in the repo. If it is compromised:

1. All encrypted files are exposed. Rotate every secret listed above.
2. Generate a new git-crypt key (requires re-initializing git-crypt on the repo — see [git-crypt docs](https://github.com/AGWA/git-crypt#using-git-crypt)).
3. Re-encrypt and force-push (history rewrite) if the attacker had access to the repo's git history.
4. Distribute the new key file out-of-band to each machine.

## History rewriting policy

This is a single-developer private repo. If a secret is accidentally committed in plaintext (outside of git-crypt), assess whether the secret is still active:

- **Active secret committed in plaintext:** Rotate the secret immediately, then decide whether to rewrite history. For a private repo with one contributor, `git filter-repo` or `BFG Repo Cleaner` is low-risk.
- **Already-rotated or expired secret:** No history rewrite needed. Record the decision here or in the commit message.
- **git-crypt-managed files:** These are ciphertext in history by design. No rewrite needed even if the repo becomes public, as long as the symmetric key is not exposed.

### Past decisions

- **`CLIProxyAPI-main/auths-backup-20260418T054556Z/`** (removed from tracking 2026-05-03): Contained expired Claude OAuth and Codex OAuth tokens from April 18 backup. Both access tokens were expired (Claude: 2026-04-18, Codex: 2026-04-28), refresh tokens belong to replaced sessions (live auth re-authenticated with different refresh tokens on 2026-04-26), and the Codex account tier changed from free to plus. No history rewrite needed — tokens cannot authenticate.
