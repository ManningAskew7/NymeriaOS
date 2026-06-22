# Release Workflow

The release pipeline lives in `.github/workflows/release.yml`. It runs on
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
the clone-free `install.sh --full` track pulls, and what a clone-free
`nymeria init` Docker setup pulls: the published compose file also ships
inside the wheel (`nymeria/setup/assets/`, kept byte-identical to
`Nymeria/docker-compose.single.published.yml` by a drift test in
`tests/test_setup_wizard.py`), and init pins `NYMERIA_VERSION` in
`.env.docker` to the installed package version, which matches the image tag
(the release tag with `v` stripped).

## Publishing The Python Package

Two independent, opt-in jobs can publish the wheel and source distribution
built by `python-package`. Both reuse that job's uploaded artifact (the exact
files that passed `twine check`, with the desktop frontend already bundled in),
so you can enable either, both, or neither.

`private-python-index` uploads to a private index with Twine. It runs on every
`v*` tag but skips itself unless the three repository secrets
`NYMERIA_PYPI_REPOSITORY_URL`, `NYMERIA_PYPI_USERNAME`, and
`NYMERIA_PYPI_PASSWORD` are set (see `docs/private/beta/BETA_PRIVATE_INDEX.md`). This
is the recommended path during a closed beta: testers install with
`uv tool install nymeriaos --index <simple-index-url>`.

`pypi-publish` uploads to public PyPI (pypi.org) using tokenless OIDC (Trusted
Publishing), so there are no secrets to store. Like the image job it is opt-in
and tag-only: it runs only when the repository variable `PUBLISH_PYPI` is `true`
AND the workflow ran on a `v*` tag, and stays dormant otherwise. This is what
lets the default `install.sh` Slim track (`uv tool install nymeriaos`) resolve
from public PyPI.

One-time setup before flipping `PUBLISH_PYPI` (no code change needed):

1. Own the `nymeriaos` name on pypi.org (register the project name, e.g. by
   publishing one release manually, so the name exists and is yours).
2. On PyPI, under the project's Manage, Publishing page, add a GitHub Trusted
   Publisher bound to this owner/repository, workflow filename `release.yml`,
   and environment name `pypi`.
3. On GitHub, under Settings, Environments, create an environment named `pypi`.
   Optionally add a protection rule so only `v*` tags (or a named approver) can
   deploy to it.
4. Set the repository variable `PUBLISH_PYPI=true`.

The job runs in the `pypi` environment with only `id-token: write` (no other
elevated scope is required, because the same-run artifact download works with
the default token), publishes with `pypa/gh-action-pypi-publish@release/v1`,
passes `skip-existing` so re-running an already-published tag is idempotent, and
emits PEP 740 attestations by default.

## Release Steps

```bash
python scripts/sync_versions.py --set 0.2.0-beta.1
git add Nymeria/nymeria/__init__.py nymeria-desktop/package.json nymeria-desktop/src-tauri/Cargo.toml nymeria-desktop/src-tauri/Cargo.lock nymeria-desktop/src-tauri/tauri.conf.json
git commit -m "Bump version to 0.2.0-beta.1"
git tag v0.2.0-beta.1
git push origin main v0.2.0-beta.1
```

After the workflow finishes, confirm the GitHub Release has the wheel, source
distribution, and Windows installer attached. If `PUBLISH_IMAGES`
is enabled, confirm the container images appear under the GHCR namespace. If
`PUBLISH_PYPI` is enabled, confirm the new version appears at
`https://pypi.org/project/nymeriaos/`.
