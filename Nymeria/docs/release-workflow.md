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
2. Verifies the tag matches the synchronized backend/desktop version with
   `python scripts/sync_versions.py --check --tag "$env:GITHUB_REF_NAME"`.
3. Sets up Rust for the Tauri build.
4. Runs `npm run tauri build` on `windows-latest`.
5. Uploads the NSIS installer from
   `nymeria-desktop/src-tauri/target/release/bundle/nsis/*.exe`.

The Windows installer is a client-only Tauri frontend. It does not build,
stage, or bundle `Nymeria/dist/nymeria-backend.exe`, and the release workflow
does not invoke PyInstaller. The PyInstaller spec remains manual-only and is
not a release gate. Testers must start or point to a separately installed
backend, then enter the backend URL and a `nym_...` account token in the
desktop setup wizard.

For a local Windows build from a checkout, install Rust and the Tauri CLI before
running `npm run tauri build`:

```powershell
cargo install tauri-cli --locked
```

The GitHub Release job downloads both artifacts and attaches the `.whl`,
`.tar.gz`, and Windows installer `.exe` files to the tag's release. Tags
containing `alpha`, `beta`, or `rc` are marked as prereleases.

The `docker-images` job builds and (optionally) publishes three container
images to GHCR: `nymeria-full`, `nymeria-slim`, and `nymeria-single`. The
job only pushes when the repository variable `PUBLISH_IMAGES` is `true` AND
the workflow ran on a `v*` tag; otherwise it builds without pushing as a
smoke test. The image namespace defaults to
`ghcr.io/${{ github.repository_owner }}` and can be overridden via the
`IMAGE_NAMESPACE` repository variable. The `nymeria-single` image is what
the clone-free `install.sh --full` track pulls.

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
The Twine upload URL is not the same as the Simple API URL that installers
pass to `pipx` or `uv tool install`.

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
configured, also check the package appears in that index. If `PUBLISH_IMAGES`
is enabled, confirm the container images appear under the GHCR namespace.
