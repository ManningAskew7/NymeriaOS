# routers/

FastAPI route slices, one router per domain, matched by filename (the list
here rotted once and was cut; `ls` is the inventory). The REST + SSE surface
is documented in `Nymeria/docs/api.md`; area traps and the webhook-bot
router pattern: the `api/` row of the backend agent guide (a maintainer doc). The
route surface and its auth contracts are ratcheted by
`tests/test_api_route_inventory.py`. Adding an endpoint: the full slice is a
factory router with injected auth, schemas, wiring in
`triggers/api.py::create_api_app`, a row in that ratchet, tests, and an entry
in `Nymeria/docs/api.md`.
