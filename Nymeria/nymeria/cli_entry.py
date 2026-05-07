"""Console-script entry point for installed Nymeria packages."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from ._runtime_paths import configure_project_root


def main(argv: Sequence[str] | None = None) -> None:
    """
    Run Nymeria's existing launcher after package-safe path bootstrap.

    ``run.py`` remains the single argparse implementation for source and
    package launches. The console script sets the writable runtime root first so
    imports inside ``run.py`` resolve config/data paths without depending on the
    user's current working directory.
    """
    configure_project_root()

    import run as run_module

    if argv is None:
        run_module.main()
        return

    previous_argv = sys.argv
    sys.argv = [previous_argv[0], *argv]
    try:
        run_module.main()
    finally:
        sys.argv = previous_argv


if __name__ == "__main__":
    main()
