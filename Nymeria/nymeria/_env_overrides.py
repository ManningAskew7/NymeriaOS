"""Single source of truth for Settings fields whose dotenv var name diverges
from the uppercased field name.

Currently only the S3 tool credentials diverge: they follow AWS SDK naming (the
boto/S3 client and the matching ``Settings.validation_alias``es use it), so the
canonical dotenv var (what ``PATCH /settings`` and ``nymeria init`` write, and
what the ``Settings`` model reads back) is the AWS name, not ``S3_*``.

Both the ``Settings`` model (``config/settings.py``, which builds the matching
``AliasChoices`` from this map) and the API write/discovery surface
(``api/schemas/settings.py::server_settings_env_mapping``) derive from this one
table so they cannot drift; kept honest by ``tests/test_settings_env_mapping.py``.

This is a dependency-free, stdlib-only top-level leaf (mirroring
``_runtime_paths.py``): both the ``config`` and ``api`` layers import it without
pulling the heavy ``config.settings`` module (importing any ``config.*``
submodule eagerly loads ``config.settings`` via the package ``__init__``, which
would defeat the schema layer's deliberate import-light stance), and it imports
no other modules, so it cannot introduce an import cycle.
"""

from __future__ import annotations

# Settings field name -> canonical dotenv var, listed only when it is NOT simply
# ``field.upper()``. The legacy ``S3_*`` spellings stay accepted via each field's
# second ``AliasChoices`` entry; this map carries only the canonical (primary) var.
FIELD_ENV_OVERRIDES: dict[str, str] = {
    "s3_access_key_id": "AWS_ACCESS_KEY_ID",
    "s3_secret_access_key": "AWS_SECRET_ACCESS_KEY",
    "s3_session_token": "AWS_SESSION_TOKEN",
    "s3_region": "AWS_REGION",
    "s3_endpoint_url": "AWS_ENDPOINT_URL_S3",
}
