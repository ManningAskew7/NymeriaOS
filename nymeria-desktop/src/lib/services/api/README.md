# API Service Modularization

This directory contains the modular structure for the Nymeria API service.

## Planned Structure

The `api.svelte.ts` file (1090 lines) should be split into:

- `base.ts` - Core NymeriaAPI class, utilities, health check
- `chat.ts` - Chat/streaming methods
- `todos.ts` - TODO operations
- `tools.ts` - Custom tools API
- `agents.ts` - Sub-agents API
- `index.ts` - Re-export combined API

## Migration Path

1. Create each module with the extracted methods
2. Update `api.svelte.ts` to import and re-export from modules
3. Existing imports (`$lib/services/api.svelte`) continue to work
4. Eventually remove the monolithic file

## Current Status

The API service currently remains in `api.svelte.ts` for backwards compatibility.
The old experimental store utility pattern was removed after it never gained
callers; future API splitting should be based on active service domains.
