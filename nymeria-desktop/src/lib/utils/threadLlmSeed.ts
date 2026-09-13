/**
 * Thread LLM route fields: the form <-> saved-config derivations.
 *
 * The thread settings panel holds its LLM form state as plain strings and
 * seeds them from the config once at mount. A server-side route apply (the
 * per-thread CLIProxy walkthrough, POST /cliproxy/apply-route with scope
 * thread) changes the saved config underneath that form, so the same
 * derivation must run again afterwards: a stale form re-sent on Save would
 * silently clobber the applied route. Keeping BOTH directions here, as pure
 * functions, means mount-time seeding, post-apply re-seeding and the Save
 * payload cannot drift apart, and the round trip (config -> fields -> config)
 * is testable without mounting the panel.
 *
 * Only the fields a route apply writes are covered (provider, model, base
 * URL, API key, API mode, provider route); generation and context fields
 * are untouched by a route and stay with the panel's own initializers.
 */

import type { ProviderRoute, ThreadConfig } from '$lib/types';
import {
  DEFAULT_CUSTOM_OPENAI_BASE_URL,
  fromThreadDisplayProvider,
  supportsOpenAiApiMode,
  toThreadDisplayProvider,
  type ThreadDisplayProvider,
} from './providerMapping';

export interface ThreadLlmRouteFields {
  threadDisplayProvider: ThreadDisplayProvider;
  llmProvider: string;
  llmModel: string;
  llmBaseUrl: string;
  llmApiKey: string;
  llmOpenAiApiMode: 'default' | 'chat_completions' | 'responses';
  llmProviderRoute: 'default' | ProviderRoute;
}

/** The route slice of a thread llm_config PATCH payload. */
export interface ThreadLlmRouteConfig {
  provider: string | null;
  model: string | null;
  base_url: string | null;
  api_key: string | null;
  openai_api_mode: 'chat_completions' | 'responses' | null;
  provider_route: ProviderRoute | null;
}

/** The "inherit everything" baseline a thread with no LLM override shows. */
export const INHERITED_LLM_ROUTE_FIELDS: ThreadLlmRouteFields = Object.freeze({
  threadDisplayProvider: '',
  llmProvider: '',
  llmModel: '',
  llmBaseUrl: '',
  llmApiKey: '',
  llmOpenAiApiMode: 'default',
  llmProviderRoute: 'default',
});

export function llmRouteFieldsFrom(
  config: ThreadConfig | null | undefined
): ThreadLlmRouteFields {
  const llm = config?.llmConfig;
  if (!llm) return { ...INHERITED_LLM_ROUTE_FIELDS };
  const provider = llm.provider ?? '';
  return {
    threadDisplayProvider: toThreadDisplayProvider(provider, llm.base_url),
    llmProvider: provider,
    llmModel: llm.model ?? '',
    llmBaseUrl: llm.base_url ?? '',
    llmApiKey: llm.api_key ?? '',
    llmOpenAiApiMode: llm.openai_api_mode ?? 'default',
    llmProviderRoute: llm.provider_route ?? 'default',
  };
}

/**
 * The Save-side inverse: route fields back into llm_config keys.
 *
 * `savedBaseUrl` is the base URL the thread config currently holds. It
 * matters for exactly one display value: "Anthropic (via proxy)" hides the
 * base URL field, so the form cannot have edited it, and its display mapping
 * says "inherit global". A saved explicit URL there (the CLIProxy
 * walkthrough's claude route writes one) must survive Save rather than
 * collapse to inherit, but only the SAVED value is kept: a stale URL typed
 * for an earlier custom-endpoint pick is not carried across.
 * `globalProvider` stands in when the thread inherits the provider.
 */
export function llmRouteConfigFrom(
  fields: ThreadLlmRouteFields,
  options: { savedBaseUrl: string | null | undefined; globalProvider: string }
): ThreadLlmRouteConfig {
  const mapped = fromThreadDisplayProvider(fields.threadDisplayProvider);
  const effectiveProvider = mapped.provider || options.globalProvider || '';
  let baseUrl: string | null;
  if (fields.threadDisplayProvider === 'openai_custom') {
    baseUrl = fields.llmBaseUrl || DEFAULT_CUSTOM_OPENAI_BASE_URL;
  } else if (supportsOpenAiApiMode(effectiveProvider)) {
    baseUrl = fields.llmBaseUrl || mapped.baseUrl;
  } else if (
    fields.threadDisplayProvider === 'anthropic_proxy'
    && fields.llmBaseUrl
    && fields.llmBaseUrl === (options.savedBaseUrl ?? '')
  ) {
    baseUrl = fields.llmBaseUrl;
  } else {
    baseUrl = mapped.baseUrl;
  }
  return {
    provider: mapped.provider || null,
    model: fields.llmModel || null,
    base_url: baseUrl,
    api_key: fields.llmApiKey || null,
    openai_api_mode:
      supportsOpenAiApiMode(effectiveProvider) && fields.llmOpenAiApiMode !== 'default'
        ? fields.llmOpenAiApiMode
        : null,
    provider_route: fields.llmProviderRoute !== 'default' ? fields.llmProviderRoute : null,
  };
}
