/**
 * Pure helpers for the onboarding setup surface.
 *
 * Everything here is side-effect free so the save/prefill logic that decides
 * WHAT gets written to the backend is unit-testable without mounting Svelte
 * components. The components own only presentation and API calls.
 */

import type {
  CLIProxyProviderInfo,
  LLMProviderSpec,
  OpenAIApiMode,
  RagCatalog,
  ServerSettings,
  ServerSettingsUpdate,
} from '$lib/types';
import type {
  ProviderSelectGroup,
  ProviderSelectOption,
} from '$lib/components/common/ProviderSelect.svelte';
import { normalizeBaseUrl } from '$lib/utils/providerMapping';

// ---------------------------------------------------------------------------
// Auth paths (mirrors the CLI wizard's ProviderAuthMethod branches)
// ---------------------------------------------------------------------------

export type SetupAuthPath = 'api_key' | 'cliproxy' | 'local';

/** Default Ollama endpoint for the local-model path (the CLI pins the same). */
export const DEFAULT_LOCAL_MODEL_BASE_URL = 'http://localhost:11434';

/**
 * Normalize a CLIProxy base URL to the shape the selected CLI expects:
 * `root` CLIs use the bare proxy origin (no /v1), `v1` CLIs need the /v1
 * suffix. Mirrors ProviderSetupWizard's getNormalizedBaseUrl.
 */
export function normalizeCliproxyBaseUrl(raw: string, shape: 'root' | 'v1'): string {
  const normalized = normalizeBaseUrl(raw);
  if (!normalized) return '';
  if (shape === 'root' && normalized.endsWith('/v1')) {
    return normalized.slice(0, -3);
  }
  if (shape === 'v1' && !normalized.endsWith('/v1')) {
    return `${normalized}/v1`;
  }
  return normalized;
}

export interface ProviderSaveInput {
  authPath: SetupAuthPath;
  /** Backend provider id (registry id for api_key, derived for the others). */
  provider: string;
  model: string;
  apiKey: string;
  /** Already-normalized base URL, or '' for the provider default. */
  baseUrl: string;
  apiMode: OpenAIApiMode | null;
  /** The selected CLIProxy catalog entry (cliproxy path only). */
  cliproxySpec: CLIProxyProviderInfo | null;
}

/**
 * Build the PATCH /settings payload for the provider section.
 *
 * Key routing:
 * - api_key path uses the generic `llm_api_key` slot; the backend routes it to
 *   the provider's declared env var (the same var the CLI finalize writes), so
 *   one code path covers the whole registry.
 * - cliproxy path keeps the dedicated gateway slots: the cpx- gatekeeper goes
 *   to the spec's key slot (ANTHROPIC_API_KEY, OPENAI_API_KEY, or
 *   GEMINI_API_KEY), never a *_DIRECT_* slot, exactly as
 *   ProviderSetupWizard saves today.
 * - local path stores no key (Ollama), only provider/model/base URL.
 */
export function buildProviderSaveUpdate(input: ProviderSaveInput): ServerSettingsUpdate {
  const updates: ServerSettingsUpdate = {
    llm_provider: input.provider,
    llm_model: input.model.trim(),
    llm_base_url: input.baseUrl || '',
  };

  if (input.apiMode) {
    updates.openai_api_mode = input.apiMode;
  }

  if (input.authPath === 'cliproxy') {
    // key_env_var -> settings field, mirroring the backend catalog invariant
    // (spec.key_setting IS a ServerSettingsUpdate field name). A hardcoded
    // openai-else-anthropic binary here once routed antigravity's gatekeeper
    // into anthropic_api_key, clobbering a real credential.
    const keySlots: Record<string, 'openai_api_key' | 'anthropic_api_key' | 'gemini_api_key'> = {
      OPENAI_API_KEY: 'openai_api_key',
      ANTHROPIC_API_KEY: 'anthropic_api_key',
      GEMINI_API_KEY: 'gemini_api_key',
    };
    const slot = keySlots[input.cliproxySpec?.key_env_var ?? ''] ?? 'anthropic_api_key';
    updates[slot] = input.apiKey.trim();
    if (input.cliproxySpec?.api_mode) {
      updates.openai_api_mode = input.cliproxySpec.api_mode as OpenAIApiMode;
    }
  } else if (input.authPath === 'api_key' && input.apiKey.trim()) {
    updates.llm_api_key = input.apiKey.trim();
  }
  // local path: no key.

  return updates;
}

/** True when the api-mode picker applies to the current provider selection. */
export function providerSupportsApiMode(spec: LLMProviderSpec | null): boolean {
  if (!spec) return false;
  return spec.supports_responses && spec.supports_chat_completions;
}

const TIER_ORDER = ['native', 'gateway', 'unverified'] as const;
const TIER_LABELS: Record<(typeof TIER_ORDER)[number], string> = {
  native: 'Native reasoning',
  gateway: 'Gateway',
  unverified: 'Unverified',
};

/**
 * Tier-grouped picker groups straight from the fetched provider registry,
 * mirroring the CLI wizard's grouped_provider_specs (native, gateway,
 * unverified; label-sorted within each). Unlike buildProviderGroups (the
 * Settings picker, which layers curated display entries and CLIProxy
 * synthetics), this renders raw registry ids because onboarding splits
 * CLIProxy and local into their own auth paths.
 */
export function registryProviderGroups(catalog: LLMProviderSpec[]): ProviderSelectGroup[] {
  if (catalog.length === 0) {
    // Offline fallback: the handful of ids every backend knows.
    return [
      {
        label: 'Providers',
        options: [
          { value: 'anthropic', label: 'Anthropic' },
          { value: 'openai', label: 'OpenAI' },
          { value: 'google', label: 'Google Gemini' },
          { value: 'openrouter', label: 'OpenRouter' },
          { value: 'ollama', label: 'Ollama local' },
        ],
      },
    ];
  }

  const buckets: Record<string, ProviderSelectOption[]> = {
    native: [],
    gateway: [],
    unverified: [],
  };
  for (const spec of catalog) {
    const tier = TIER_ORDER.includes(spec.tier as (typeof TIER_ORDER)[number])
      ? (spec.tier as (typeof TIER_ORDER)[number])
      : 'unverified';
    buckets[tier].push({
      value: spec.id,
      label: spec.label,
      tier,
      notesForUser: spec.notes_for_user ?? '',
    });
  }
  for (const tier of TIER_ORDER) {
    buckets[tier].sort((a, b) => a.label.localeCompare(b.label));
  }
  return TIER_ORDER.map((tier) => ({
    label: TIER_LABELS[tier],
    tier,
    options: buckets[tier],
  })).filter((g) => g.options.length > 0);
}

/**
 * Recover which CLIProxy catalog entry a saved settings pair points at, so the
 * section prefills correctly for a CLI-configured backend. Shared with
 * ProviderSetupWizard: anthropic = claude; a Responses-mode openai route =
 * codex; otherwise resolve the chat-mode CLI by its catalog default model,
 * falling back to the first chat-mode entry.
 */
export function detectCliproxyEntry(
  catalog: CLIProxyProviderInfo[],
  settings: Pick<ServerSettings, 'llm_provider' | 'llm_model' | 'openai_api_mode'>
): string {
  if (settings.llm_provider === 'anthropic') return 'claude';
  if (settings.llm_provider === 'google') {
    // The native-Gemini CLIProxy shape (antigravity since 2026-08-07).
    const googleEntry = catalog.find((entry) => entry.nymeria_provider === 'google');
    return googleEntry?.id ?? 'antigravity';
  }
  if ((settings.openai_api_mode ?? 'responses') === 'responses') return 'codex';
  const model = (settings.llm_model || '').trim();
  const byModel = catalog.find(
    (entry) => entry.api_mode === 'chat_completions' && entry.default_model === model
  );
  if (byModel) return byModel.id;
  const chatEntry = catalog.find((entry) => entry.api_mode === 'chat_completions');
  return chatEntry?.id ?? 'codex';
}

/** Derive which auth path the saved settings represent, for section prefill. */
export function detectAuthPath(
  settings: Pick<ServerSettings, 'llm_provider' | 'llm_base_url'>
): SetupAuthPath {
  if (settings.llm_provider === 'ollama') return 'local';
  if (
    (settings.llm_provider === 'anthropic'
      || settings.llm_provider === 'openai'
      || settings.llm_provider === 'google') &&
    settings.llm_base_url
  ) {
    return 'cliproxy';
  }
  return 'api_key';
}

// ---------------------------------------------------------------------------
// Agent section: context strategy (mirrors the CLI's CONTEXT_CHOICES)
// ---------------------------------------------------------------------------

export type ContextStrategy = 'compact_tokens' | 'compact_percent' | 'sliding_window' | 'none';

/** Derive the CLI-style strategy id from the saved settings pair. */
export function contextStrategyFromSettings(
  settings: Pick<ServerSettings, 'context_management' | 'compact_threshold_mode'>
): ContextStrategy {
  if (settings.context_management === 'none') return 'none';
  if (settings.context_management === 'sliding_window') return 'sliding_window';
  return settings.compact_threshold_mode === 'percentage' ? 'compact_percent' : 'compact_tokens';
}

/** Build the PATCH payload for a strategy pick plus its trigger value. */
export function buildContextUpdate(
  strategy: ContextStrategy,
  trigger: { tokens?: number; percent?: number; cycles?: number }
): ServerSettingsUpdate {
  switch (strategy) {
    case 'compact_tokens':
      return {
        context_management: 'auto_compact',
        compact_threshold_mode: 'tokens',
        ...(trigger.tokens ? { compact_threshold_tokens: trigger.tokens } : {}),
      };
    case 'compact_percent':
      return {
        context_management: 'auto_compact',
        compact_threshold_mode: 'percentage',
        // The API stores a 0.05-0.95 fraction; the UI shows whole percent.
        ...(trigger.percent ? { compact_threshold: trigger.percent / 100 } : {}),
      };
    case 'sliding_window':
      return {
        context_management: 'sliding_window',
        ...(trigger.cycles ? { sliding_window_cycles: trigger.cycles } : {}),
      };
    case 'none':
      return { context_management: 'none' };
  }
}

/** The browser's IANA timezone, or '' when unavailable. */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? '';
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------------------
// RAG section: catalog id <-> settings mapping (mirrors rag_catalog.py's
// embedder_id_for_env / reranker_id_for_env, driven by the FETCHED catalog so
// there is no hand-copied table to drift)
// ---------------------------------------------------------------------------

export function embedderIdForSettings(
  catalog: RagCatalog,
  settings: Pick<ServerSettings, 'embedding_provider' | 'embedding_model' | 'embedding_dimensions'>
): string | null {
  const { embedding_provider: provider, embedding_model: model } = settings;
  if (!provider || !model) return null;
  const dims = settings.embedding_dimensions;
  if (dims != null) {
    const exact = catalog.embedders.find(
      (o) => o.provider === provider && o.model === model && o.dimensions === dims
    );
    if (exact) return exact.id;
  }
  const byModel = catalog.embedders.find((o) => o.provider === provider && o.model === model);
  return byModel?.id ?? null;
}

export function rerankerIdForSettings(
  catalog: RagCatalog,
  settings: Pick<ServerSettings, 'rag_rerank_enabled' | 'rag_rerank_provider' | 'rag_rerank_model'>
): string | null {
  if (!settings.rag_rerank_enabled) {
    return catalog.rerankers.find((o) => o.provider === 'none')?.id ?? null;
  }
  const provider = settings.rag_rerank_provider;
  if (!provider) return null;
  const model = settings.rag_rerank_model ?? null;
  const exact = catalog.rerankers.find(
    (o) => o.provider === provider && (o.model ?? null) === model
  );
  if (exact) return exact.id;
  return catalog.rerankers.find((o) => o.provider === provider)?.id ?? null;
}

export interface RagSaveInput {
  catalog: RagCatalog;
  embedderId: string;
  rerankerId: string;
  retrievalMode: 'hybrid' | 'vector';
  embeddingKey: string;
  rerankKey: string;
}

/**
 * Build the PATCH payload for a RAG selection. Mirrors rag_env_for_state:
 * the embedder writes provider/model/dimensions plus base URL and input type
 * (explicit nulls CLEAR stale endpoint overrides on a provider switch), the
 * reranker toggles rag_rerank_enabled and writes provider/model. Keys ride
 * along only when the user typed one.
 */
export function buildRagUpdate(input: RagSaveInput): ServerSettingsUpdate | null {
  const embedder = input.catalog.embedders.find((o) => o.id === input.embedderId);
  if (!embedder) return null;
  const reranker = input.catalog.rerankers.find((o) => o.id === input.rerankerId) ?? null;

  const updates: ServerSettingsUpdate = {
    embedding_provider: embedder.provider,
    embedding_model: embedder.model,
    embedding_dimensions: embedder.dimensions,
    embedding_base_url: embedder.base_url ?? null,
    embedding_input_type: embedder.input_type ?? null,
    rag_retrieval_mode: input.retrievalMode,
  };

  if (reranker && reranker.provider !== 'none') {
    updates.rag_rerank_enabled = true;
    updates.rag_rerank_provider = reranker.provider;
    updates.rag_rerank_model = reranker.model ?? null;
  } else {
    updates.rag_rerank_enabled = false;
  }

  if (input.embeddingKey.trim()) updates.embedding_api_key = input.embeddingKey.trim();
  if (input.rerankKey.trim()) updates.rag_rerank_api_key = input.rerankKey.trim();

  return updates;
}
