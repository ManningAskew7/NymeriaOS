# Credential Vault

Nymeria stores reusable tool credentials in an encrypted server-side vault. The
vault is backed by `data/accounts.db` and encrypts secret fields with
`NYMERIA_SECRETS_KEY` via `nymeria/core/secrets.py`.

## Model

Credentials have public metadata and separate encrypted secret fields:

- `owner_type`: `user` or `system`
- `owner_user_id`: set for user-owned credentials
- `provider` / `kind`: examples include `google_calendar`, `microsoft`,
  `api_key`, `env_var`, and `legacy_token_cache`
- `metadata`, `scopes`, `expires_at`, `status`, `allowed_targets`
- secret field names only, never plaintext or ciphertext in API responses

`allowed_targets` scopes runtime injection. Supported target strings include
`mcp_server:<server_id>`, `custom_tool:<tool_id>`, wildcard forms like
`mcp_server:*`, `llm_provider:<provider-id>`, `llm_provider:*`, or `*`.

## APIs

Primary routes:

- `GET /credentials?scope=visible|mine|system|all`
- `POST /credentials`
- `GET /credentials/{credential_id}`
- `PATCH /credentials/{credential_id}`
- `DELETE /credentials/{credential_id}`
- `POST /credentials/{credential_id}/test`
- `GET /credentials/{credential_id}/bindings`
- `POST /credentials/{credential_id}/bindings`
- `DELETE /credential-bindings/{binding_id}`
- `POST /credential-setup-sessions`

Writes accept `secret_fields` as plaintext request fields. Responses only return
metadata and the list of stored secret field names.

## Agent Access

The optional `auth_manager` tool is metadata-only. It can list credentials,
create pending setup records, and manage user-owned bindings/tests/disables.
It cannot manage system credentials or retrieve plaintext secret fields.

Agents should use `auth_manager(action="request_setup", ...)` when the user
needs to enter a key. The frontend Settings -> Connections panel completes the
pending setup record through an authenticated API call, keeping secret material
out of chat history.

## Runtime Injection

Custom HTTP tools and MCP server env/header config can reference stored secrets:

```text
${credential:cred_abc123.value}
```

The backend resolves this only during execution, checks the credential's
allowed target, records the credential ID in audit metadata, and redacts
resolved secret values in HTTP audit logs and request metadata. Tool results
and API responses do not include the secret value.

LLM providers also use the credential vault. Save a credential with
`provider=<LLM provider ID>` such as `openai`, `openrouter`, `groq`, or
`lmstudio`; `kind=api_key` or `llm_provider`; secret field `api_key`; and
allowed target `llm_provider:<provider-id>` or `llm_provider:*`. A credential
may also include a secret or metadata `base_url` field for custom endpoints.
Bindings improve selection order, but the allowed target is still what permits
secret access.

## Migration

On backend startup, Nymeria imports existing first-level per-user auth caches
from `data/auth_tokens/<user_id>/*.json` into `legacy_token_cache` credentials.
Provider-specific exported MCP files under `data/auth_tokens/<user_id>/mcp/`
are left in place because third-party MCP servers still need those files.

Legacy managed MCP `encrypted_env_vars` are migrated to system credentials and
the server definition is rewritten to use `${credential:...}` env references.
If decryption or import fails, the original value is left untouched and a
non-fatal warning is logged.

## Operational Notes

If `NYMERIA_SECRETS_KEY` is missing, new vault secret writes fail for vault APIs.
Existing auth-cache helpers fall back to legacy file storage so local dev flows
do not break abruptly, but production deployments should configure the key.

Fernet key rotation still requires decrypting and re-encrypting stored values;
there is no automated rotation command yet.
