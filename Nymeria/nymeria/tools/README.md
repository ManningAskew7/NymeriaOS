# tools/

Agent tool implementations. How to add or classify a tool: the `__init__.py`
module docstring is canonical, and the tool-group registry in `registry.py`
derives the catalog, count, and role gates (nothing is hand-listed). The
per-tool module map is the generated
`Nymeria/docs/agent-systems/tools-index.md`. Area traps and invariants:
the `tools/` row of the backend agent guide (a maintainer doc) and the
agent-systems docs under `Nymeria/docs/agent-systems/`.
