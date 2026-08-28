# routers/

FastAPI route slices, one router per domain, matched by filename (the list
here rotted once and was cut; `ls` is the inventory). The REST + SSE surface
is documented in `Nymeria/docs/api.md`; area traps and the webhook-bot
router pattern: the backend guide's `api/` row (`Nymeria/CLAUDE.md`). The
route surface and its auth contracts are ratcheted by
`tests/test_api_route_inventory.py`. Adding an endpoint: the full slice
(factory shape, DI'd auth, ratchet row, docs) is
`Nymeria/docs/private/rest-endpoint-recipe.md`.
