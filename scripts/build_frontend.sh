#!/usr/bin/env bash
# Rebuild the served web UI from source and bundle it into the Python package.
#
# Mirrors .github/workflows/release.yml's "Build desktop frontend" and
# "Bundle frontend into Python package" steps so a source install can refresh
# the UI `nymeria api` serves without reading CI YAML. Safe to run from any
# directory; it operates on the repo this script lives in.
#
# The Docker source stack needs this bundle step too: the API serves the
# package copy Nymeria/nymeria/frontend/ (bind-mounted via ./nymeria) whenever
# it holds a build, and the nymeria-desktop/build mount at /app/frontend is
# only a fallback for when it does not. Run this script, then restart the api
# container (no image rebuild needed).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT/nymeria-desktop"
npm install
npm run build

cd "$ROOT"
rm -rf Nymeria/nymeria/frontend
mkdir -p Nymeria/nymeria/frontend
cp -a nymeria-desktop/build/. Nymeria/nymeria/frontend/
test -f Nymeria/nymeria/frontend/index.html
test -d Nymeria/nymeria/frontend/_app

echo "Frontend bundled into Nymeria/nymeria/frontend/. Restart the backend to serve it."
