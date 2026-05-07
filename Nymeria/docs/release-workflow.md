# Release Workflow

The beta release pipeline lives in `.github/workflows/release.yml`. It runs on
tags matching `v*` and can also be started manually from GitHub Actions.

## What The Workflow Builds

The `python-package` job builds one set of Python distribution artifacts:

1. Installs the desktop frontend dependencies with Node 20.
2. Runs `npm run build` in `nymeria-desktop/`.
3. Copies `nymeria-desktop/build/` into `Nymeria/nymeria/frontend/`, which is
   the package-data location shipped by `pyproject.toml`.
4. Verifies the tag matches the synchronized backend/desktop version with
   `python scripts/sync_versions.py --check --tag "$GITHUB_REF_NAME"`.
5. Builds the wheel and source distribution with `python -m build`.
6. Runs `twine check` before uploading artifacts.

The GitHub Release job downloads that artifact and attaches the `.whl` and
`.tar.gz` files to the tag's release. Tags containing `alpha`, `beta`, or `rc`
are marked as prereleases.

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

## Release Steps

```bash
python scripts/sync_versions.py --set 0.2.0-beta.1
git add Nymeria/nymeria/__init__.py nymeria-desktop/package.json nymeria-desktop/src-tauri/Cargo.toml nymeria-desktop/src-tauri/Cargo.lock nymeria-desktop/src-tauri/tauri.conf.json
git commit -m "Bump version to 0.2.0-beta.1"
git tag v0.2.0-beta.1
git push origin main v0.2.0-beta.1
```

After the workflow finishes, confirm the GitHub Release has both the wheel and
source distribution attached. If private index secrets are configured, also
check the package appears in that index before sending installer instructions
to beta testers.
