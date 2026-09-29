"""FastAPI route modules for Nymeria.

The REST + SSE surface is documented in ``docs/api.md``; router map
and area traps: the ``api/`` row of the backend agent guide (a maintainer doc).
Adding an endpoint: a factory router with injected auth, schemas, wiring in
``triggers/api.py::create_api_app``, a row in the route-inventory ratchet
(``tests/test_api_route_inventory.py``), tests, and an entry in
``docs/api.md``.
"""
