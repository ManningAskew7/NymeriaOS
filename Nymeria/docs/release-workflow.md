# Release Workflow

The beta release pipeline lives in `.github/workflows/release.yml`. It runs on
tags matching `v*` and can also be started manually from GitHub Actions.

## What The Workflow Builds

The `python-package` job builds the Python distribution artifacts:

1. Installs the desktop frontend dependencies with Node 20.
2. Runs `npm run build` in `nymeria-desktop/`.
3. Copies `nymeria-desktop/build/` into `Nymeria/nymeria/frontend/`, which is
   the package-data location shipped by `pyproject.toml`.
4. Verifies the tag matches the synchronized backend/desktop version with
   `python scripts/sync_versions.py --check --tag "$GITHUB_REF_NAME"`.
5. Builds the wheel and source distribution with `python -m build`.
6. Runs `twine check` before uploading artifacts.

The `windows-desktop` job builds the Windows desktop installer:

1. Installs the desktop frontend dependencies with Node 20.
2. Builds the Svelte frontend and copies it into `Nymeria/nymeria/frontend/`.
3. Installs the backend package and PyInstaller dependencies.
4. Builds `Nymeria/dist/nymeria-backend.exe` with
   `Nymeria/nymeria-backend.spec`.
5. Runs `npm run tauri build` on `windows-latest`.
6. Uploads the NSIS installer from
   `nymeria-desktop/src-tauri/target/release/bundle/nsis/*.exe`.

The Tauri config bundles `Nymeria/dist/nymeria-backend.exe` as a resource at
`Nymeria/dist/nymeria-backend.exe` inside the installed app. At runtime, the
desktop process manager first supports source-checkout launches, then checks
the installed app's `resources` directory for that bundled backend. Source
checkouts keep using `Nymeria/.env`; installed desktop builds use the writable
`~/.nymeria/config.env` and `~/.nymeria/data/` runtime convention.

The GitHub Release job downloads both artifacts and attaches the `.whl`,
`.tar.gz`, and Windows installer `.exe` files to the tag's release. Tags
containing `alpha`, `beta`, or `rc` are marked as prereleases.

## Private Python Index

GitHub Packages does not provide a PyPI-compatible package registry. Its
[documented package registries](https://docs.github.com/en/enterprise-server@3.20/packages/learn-github-packages/introduction-to-github-packages#support-for-package-registries)
are npm, RubyGems, Maven/Gradle, NuGet, and Docker/container images. For beta
Python package installs, use a private index that accepts Twine/Warehouse
uploads, or distribute the wheel from the private GitHub Release.

The `private-python-index` job publishes only when these repository secrets are
set:

| Secret | Purpose |
|--------|---------|
| `NYMERIA_PYPI_REPOSITORY_URL` | Upload endpoint, for example `https://upload.pypi.org/legacy/` or a private registry's legacy upload URL |
| `NYMERIA_PYPI_USERNAME` | Registry username, or `__token__` for token-based registries |
| `NYMERIA_PYPI_PASSWORD` | Registry password or API token |

If any of those secrets are missing, the job logs a skip message and succeeds.
The Twine upload URL is not the same as the Simple API URL that testers pass to
`pipx`. See [BETA_PRIVATE_INDEX.md](./BETA_PRIVATE_INDEX.md) for maintainer
setup, tester install commands, and the GitHub Release fallback.

## Release Steps

```bash
python scripts/sync_versions.py --set 0.2.0-beta.1
git add Nymeria/nymeria/__init__.py nymeria-desktop/package.json nymeria-desktop/src-tauri/Cargo.toml nymeria-desktop/src-tauri/Cargo.lock nymeria-desktop/src-tauri/tauri.conf.json
git commit -m "Bump version to 0.2.0-beta.1"
git tag v0.2.0-beta.1
git push origin main v0.2.0-beta.1
```

After the workflow finishes, confirm the GitHub Release has the wheel, source
distribution, and Windows installer attached. If private index secrets are
configured, also check the package appears in that index before sending
installer instructions to beta testers.

Grant tester access with
[BETA_ACCESS_CONTROL.md](./BETA_ACCESS_CONTROL.md): invite GitHub users as
read-only collaborators for private Release assets, issue private-index
read/download credentials, and record revocation status outside the repository.
