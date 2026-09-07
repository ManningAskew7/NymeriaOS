#!/usr/bin/env python3
"""Fail unless a built nymeriaos wheel carries every runtime data file.

A wheel ships only what ``[tool.setuptools.package-data]`` declares, so a
data file the code needs but nobody listed exists in every source checkout
and in no wheel: ``nymeria init`` on the 0.2.0b1 wheel crashed on its first
screen because ``setup/theme.tcss`` was never declared (2026-09-07). The
release workflow runs this on the wheel it just built, before twine and
before anything is uploaded; run it locally on the output of
``python -m build --wheel``.

Usage: ``python scripts/check_wheel_contents.py dist/nymeriaos-*.whl``
"""

from __future__ import annotations

import fnmatch
import sys
import zipfile

# (pattern, why). Each pattern must match at least one entry in the wheel.
REQUIRED: tuple[tuple[str, str], ...] = (
    ("nymeria/setup/theme.tcss", "the wizard's Textual stylesheet (setup/app.py CSS_PATH)"),
    ("nymeria/setup/assets/*.yml", "the wizard's compose and searxng assets"),
    ("nymeria/frontend/index.html", "the served web UI entry"),
    ("nymeria/frontend/_app/immutable/entry/*.js", "the web UI's SvelteKit entry chunks"),
    ("nymeria/config/soul.md", "the default persona prompt"),
    ("nymeria/config/data/*.json", "the bundled model and provider catalogs"),
    ("nymeria/hooks_bundled/*.json", "the bundled lifecycle hooks"),
    ("nymeria/skills_bundled/*/SKILL.md", "the bundled skills"),
    ("nymeria/workflows_bundled/*.json", "the bundled workflows"),
)


def missing_entries(names: list[str]) -> list[tuple[str, str]]:
    """Return the (pattern, why) pairs no wheel entry satisfies."""
    return [(pat, why) for pat, why in REQUIRED if not any(fnmatch.fnmatchcase(n, pat) for n in names)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    with zipfile.ZipFile(argv[1]) as wheel:
        names = wheel.namelist()
    missing = missing_entries(names)
    for pat, why in missing:
        print(f"MISSING {pat}: {why}", file=sys.stderr)
    if missing:
        print(f"{argv[1]}: {len(missing)} required data pattern(s) absent; add them to "
              "[tool.setuptools.package-data] in pyproject.toml", file=sys.stderr)
        return 1
    print(f"{argv[1]}: all {len(REQUIRED)} required data patterns present ({len(names)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
