"""First-run setup for packaged Nymeria installations.

A Textual step-wizard for interactive setup plus a headless finalize path for
non-interactive/flag-driven setup. See `runner.run_init` for the entry point.
"""

from __future__ import annotations

from .runner import add_init_arguments, build_parser, main, run_init

__all__ = ["add_init_arguments", "build_parser", "main", "run_init"]
