# tools/

Agent tool implementations. How to add or classify a tool: the `__init__.py`
module docstring is canonical, and the tool-group registry in `registry.py`
derives the catalog, count, and role gates (nothing is hand-listed). The
per-tool module map is the generated
`Nymeria/docs/agent-systems/tools-index.md`. Area traps and invariants:
the backend guide's `tools/` row (`Nymeria/CLAUDE.md`) and the
`public/agent-systems/` docs it names.
