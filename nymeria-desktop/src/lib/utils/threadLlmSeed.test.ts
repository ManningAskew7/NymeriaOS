import { describe, expect, it } from 'vitest';

import type { ThreadConfig } from '$lib/types';
import { INHERITED_LLM_ROUTE_FIELDS, llmRouteConfigFrom, llmRouteFieldsFrom } from './threadLlmSeed';

function configWith(llmConfig: ThreadConfig['llmConfig']): ThreadConfig {
  return { threadId: 't-1', llmConfig } as ThreadConfig;
}

describe('llmRouteFieldsFrom', () => {
  it('returns the inherit baseline for a missing config or LLM override', () => {
    expect(llmRouteFieldsFrom(null)).toEqual(INHERITED_LLM_ROUTE_FIELDS);
    expect(llmRouteFieldsFrom(undefined)).toEqual(INHERITED_LLM_ROUTE_FIELDS);
    expect(llmRouteFieldsFrom(configWith(null))).toEqual(INHERITED_LLM_ROUTE_FIELDS);
  });

  it('never hands back the shared baseline object itself', () => {
    const fields = llmRouteFieldsFrom(null);
    fields.llmModel = 'mutated';
    expect(INHERITED_LLM_ROUTE_FIELDS.llmModel).toBe('');
  });

  it('maps a Codex CLIProxy thread route (openai + /v1 URL) onto the custom-endpoint display value', () => {
    // The exact shape POST /cliproxy/apply-route scope=thread writes for the
    // codex target: provider openai, v1-shape base URL, Responses API, the
    // cpx gatekeeper as the thread key, no provider route pin.
    const fields = llmRouteFieldsFrom(
      configWith({
        provider: 'openai',
        model: 'gpt-6-astra',
        base_url: 'http://cli-proxy-api:8317/v1',
        api_key: 'cpx-nymeria-abc',
        openai_api_mode: 'responses',
        provider_route: null,
      })
    );
    expect(fields).toEqual({
      threadDisplayProvider: 'openai_custom',
      llmProvider: 'openai',
      llmModel: 'gpt-6-astra',
      llmBaseUrl: 'http://cli-proxy-api:8317/v1',
      llmApiKey: 'cpx-nymeria-abc',
      llmOpenAiApiMode: 'responses',
      llmProviderRoute: 'default',
    });
  });

  it('maps a Claude CLIProxy thread route (anthropic + root URL) onto the proxy display value', () => {
    const fields = llmRouteFieldsFrom(
      configWith({
        provider: 'anthropic',
        model: 'claude-opus-5',
        base_url: 'http://cli-proxy-api:8317',
        api_key: 'cpx-nymeria-abc',
        openai_api_mode: null,
        provider_route: null,
      })
    );
    expect(fields.threadDisplayProvider).toBe('anthropic_proxy');
    expect(fields.llmOpenAiApiMode).toBe('default');
    expect(fields.llmBaseUrl).toBe('http://cli-proxy-api:8317');
  });

  it('keeps a pinned provider route and the direct-anthropic empty base URL distinct from inherit', () => {
    const fields = llmRouteFieldsFrom(
      configWith({
        provider: 'anthropic',
        model: 'claude-sonnet-5',
        base_url: '',
        provider_route: 'anthropic_messages',
      })
    );
    expect(fields.threadDisplayProvider).toBe('anthropic_direct');
    expect(fields.llmProviderRoute).toBe('anthropic_messages');
    expect(fields.llmBaseUrl).toBe('');
  });
});

// The invariant the per-thread CLIProxy walkthrough rests on: a route the
// backend applied server-side, read into the form, Saves back as the SAME
// route. Each case is the exact shape POST /cliproxy/apply-route writes for
// that target (api/routers/cliproxy.py::_apply_route_thread).
describe('llmRouteConfigFrom round trip after a route apply', () => {
  const CODEX_ROUTE = {
    provider: 'openai',
    model: 'gpt-6-astra',
    base_url: 'http://cli-proxy-api:8317/v1',
    api_key: 'cpx-nymeria-abc',
    openai_api_mode: 'responses' as const,
    provider_route: null,
  };
  const CLAUDE_ROUTE = {
    provider: 'anthropic',
    model: 'claude-opus-5',
    base_url: 'http://cli-proxy-api:8317',
    api_key: 'cpx-nymeria-abc',
    openai_api_mode: null,
    provider_route: null,
  };

  function roundTrip(route: typeof CODEX_ROUTE | typeof CLAUDE_ROUTE, globalProvider: string) {
    const saved = configWith(route);
    return llmRouteConfigFrom(llmRouteFieldsFrom(saved), {
      savedBaseUrl: saved.llmConfig?.base_url,
      globalProvider,
    });
  }

  it('reproduces a codex thread route exactly', () => {
    expect(roundTrip(CODEX_ROUTE, 'anthropic')).toEqual(CODEX_ROUTE);
  });

  it('reproduces a claude thread route exactly, keeping the hidden proxy root URL', () => {
    // The "Anthropic (via proxy)" display value hides the base URL field and
    // maps to "inherit"; without the saved-URL carry-over Save dropped the
    // route's root URL while keeping its cpx key (review finding).
    expect(roundTrip(CLAUDE_ROUTE, 'openai')).toEqual(CLAUDE_ROUTE);
  });

  it('does not carry a stale URL onto an anthropic-via-proxy pick', () => {
    // A /v1 URL typed for an earlier custom-endpoint pick is NOT the saved
    // anthropic URL, so switching the display provider must fall back to
    // inherit rather than send the Anthropic SDK to .../v1/v1/messages.
    const fields = {
      ...llmRouteFieldsFrom(configWith(CLAUDE_ROUTE)),
      llmBaseUrl: 'http://cli-proxy-api:8317/v1',
    };
    const config = llmRouteConfigFrom(fields, {
      savedBaseUrl: CLAUDE_ROUTE.base_url,
      globalProvider: 'anthropic',
    });
    expect(config.base_url).toBeNull();
    expect(config.provider).toBe('anthropic');
  });

  it('writes an all-null route slice for the inherit baseline', () => {
    expect(
      llmRouteConfigFrom({ ...INHERITED_LLM_ROUTE_FIELDS }, { savedBaseUrl: null, globalProvider: 'anthropic' })
    ).toEqual({
      provider: null,
      model: null,
      base_url: null,
      api_key: null,
      openai_api_mode: null,
      provider_route: null,
    });
  });

  it('keeps the custom-endpoint default URL and drops API mode for non-OpenAI providers', () => {
    const custom = llmRouteConfigFrom(
      { ...INHERITED_LLM_ROUTE_FIELDS, threadDisplayProvider: 'openai_custom', llmOpenAiApiMode: 'responses' },
      { savedBaseUrl: null, globalProvider: 'anthropic' }
    );
    expect(custom.base_url).toBe('http://cli-proxy-api-latest:8317/v1');
    expect(custom.openai_api_mode).toBe('responses');
    const direct = llmRouteConfigFrom(
      { ...INHERITED_LLM_ROUTE_FIELDS, threadDisplayProvider: 'anthropic_direct', llmOpenAiApiMode: 'responses' },
      { savedBaseUrl: null, globalProvider: 'openai' }
    );
    expect(direct.base_url).toBe('');
    expect(direct.openai_api_mode).toBeNull();
  });
});
