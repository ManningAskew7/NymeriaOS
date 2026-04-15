# Secrets Management — git-crypt

As of commit `936cd89`, secret files live **inside** the repo, encrypted at rest with [git-crypt](https://github.com/AGWA/git-crypt).

## Encrypted Files

Defined in `.gitattributes`:

- `Nymeria/.env.docker` — Docker deployment secrets
- `Nymeria/.env` — Local dev secrets (when present)
- `Nymeria/firebase-service-account.json` — FCM push credentials
- `Nymeria/google_credentials.json` — Google OAuth credentials

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
