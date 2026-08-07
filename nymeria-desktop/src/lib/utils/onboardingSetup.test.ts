import { describe, expect, it } from 'vitest';
import type { CLIProxyProviderInfo, LLMProviderSpec, RagCatalog } from '$lib/types';
import {
  buildContextUpdate,
  buildProviderSaveUpdate,
  buildRagUpdate,
  contextStrategyFromSettings,
  detectAuthPath,
  detectCliproxyEntry,
  embedderIdForSettings,
  normalizeCliproxyBaseUrl,
  registryProviderGroups,
  rerankerIdForSettings,
} from './onboardingSetup';

function cliproxyEntry(overrides: Partial<CLIProxyProviderInfo>): CLIProxyProviderInfo {
  return {
    id: 'claude',
    label: 'Claude',
    description: '',
    flow: 'browser',
    nymeria_provider: 'anthropic',
    url_shape: 'root',
    api_mode: '',
    key_env_var: 'ANTHROPIC_API_KEY',
    default_model: 'claude-opus-4-7',
    tos_warning: '',
    auth_file_provider: 'claude',
    supported: null,
    logged_in: null,
    ...overrides,
  };
}

describe('normalizeCliproxyBaseUrl', () => {
  it('strips /v1 for root-shape CLIs and appends it for v1-shape CLIs', () => {
    expect(normalizeCliproxyBaseUrl('http://localhost:8317/v1', 'root')).toBe(
      'http://localhost:8317'
    );
    expect(normalizeCliproxyBaseUrl('http://localhost:8317', 'v1')).toBe(
      'http://localhost:8317/v1'
    );
    expect(normalizeCliproxyBaseUrl('http://localhost:8317/v1/', 'v1')).toBe(
      'http://localhost:8317/v1'
    );
  });
});

describe('buildProviderSaveUpdate', () => {
  it('routes api_key-path keys through the generic llm_api_key slot', () => {
    const updates = buildProviderSaveUpdate({
      authPath: 'api_key',
      provider: 'groq',
      model: 'llama-test',
      apiKey: ' gsk-secret ',
      baseUrl: '',
      apiMode: null,
      cliproxySpec: null,
    });
    expect(updates.llm_provider).toBe('groq');
    expect(updates.llm_api_key).toBe('gsk-secret');
    expect(updates.anthropic_api_key).toBeUndefined();
    expect(updates.openai_api_key).toBeUndefined();
    expect(updates.llm_base_url).toBe('');
  });

  it('keeps the cpx gatekeeper in the gateway slots, never the direct ones', () => {
    const claude = buildProviderSaveUpdate({
      authPath: 'cliproxy',
      provider: 'anthropic',
      model: 'claude-opus-4-7',
      apiKey: 'cpx-key',
      baseUrl: 'http://localhost:8318',
      apiMode: null,
      cliproxySpec: cliproxyEntry({}),
    });
    expect(claude.anthropic_api_key).toBe('cpx-key');
    expect(claude.llm_api_key).toBeUndefined();

    const codex = buildProviderSaveUpdate({
      authPath: 'cliproxy',
      provider: 'openai',
      model: 'gpt-5.5',
      apiKey: 'cpx-key',
      baseUrl: 'http://localhost:8317/v1',
      apiMode: 'responses',
      cliproxySpec: cliproxyEntry({
        id: 'codex',
        nymeria_provider: 'openai',
        url_shape: 'v1',
        api_mode: 'responses',
        key_env_var: 'OPENAI_API_KEY',
      }),
    });
    expect(codex.openai_api_key).toBe('cpx-key');
    expect(codex.openai_api_mode).toBe('responses');
    expect(codex.llm_api_key).toBeUndefined();
  });

  it('stores no key on the local path', () => {
    const updates = buildProviderSaveUpdate({
      authPath: 'local',
      provider: 'ollama',
      model: 'qwen3:8b',
      apiKey: '',
      baseUrl: 'http://localhost:11434',
      apiMode: null,
      cliproxySpec: null,
    });
    expect(updates.llm_provider).toBe('ollama');
    expect(updates.llm_api_key).toBeUndefined();
    expect(updates.anthropic_api_key).toBeUndefined();
  });
});

describe('context strategy mapping', () => {
  it('derives the CLI-style strategy from the settings pair', () => {
    expect(
      contextStrategyFromSettings({ context_management: 'auto_compact', compact_threshold_mode: 'tokens' })
    ).toBe('compact_tokens');
    expect(
      contextStrategyFromSettings({ context_management: 'auto_compact', compact_threshold_mode: 'percentage' })
    ).toBe('compact_percent');
    expect(
      contextStrategyFromSettings({ context_management: 'sliding_window', compact_threshold_mode: 'tokens' })
    ).toBe('sliding_window');
    expect(
      contextStrategyFromSettings({ context_management: 'none', compact_threshold_mode: 'tokens' })
    ).toBe('none');
  });

  it('converts the percent trigger to the stored 0..1 fraction', () => {
    expect(buildContextUpdate('compact_percent', { percent: 80 })).toEqual({
      context_management: 'auto_compact',
      compact_threshold_mode: 'percentage',
      compact_threshold: 0.8,
    });
    expect(buildContextUpdate('compact_tokens', { tokens: 250000 })).toEqual({
      context_management: 'auto_compact',
      compact_threshold_mode: 'tokens',
      compact_threshold_tokens: 250000,
    });
    expect(buildContextUpdate('none', {})).toEqual({ context_management: 'none' });
  });
});

const CATALOG: RagCatalog = {
  embedders: [
    {
      id: 'premium-cohere',
      tier: 'premium',
      label: 'Cohere embed-v4 (1024-d)',
      description: '',
      provider: 'cohere',
      model: 'embed-v4.0',
      dimensions: 1024,
      requires_key: true,
      key_vendor: 'cohere',
      base_url: null,
      input_type: null,
      pricing: '',
      key_label: 'Cohere API key',
      eval_tag: '',
    },
    {
      id: 'premium-cohere-1536',
      tier: 'premium',
      label: 'Cohere embed-v4 (1536-d)',
      description: '',
      provider: 'cohere',
      model: 'embed-v4.0',
      dimensions: 1536,
      requires_key: true,
      key_vendor: 'cohere',
      base_url: null,
      input_type: null,
      pricing: '',
      key_label: 'Cohere API key',
      eval_tag: '',
    },
    {
      id: 'premium-voyage-large',
      tier: 'premium',
      label: 'Voyage 4 large',
      description: '',
      provider: 'openai',
      model: 'voyage-4-large',
      dimensions: 1024,
      requires_key: true,
      key_vendor: 'voyage',
      base_url: 'https://api.voyageai.com/v1',
      input_type: 'voyage',
      pricing: '',
      key_label: 'Voyage API key',
      eval_tag: '',
    },
  ],
  rerankers: [
    {
      id: 'none',
      tier: 'none',
      label: 'No reranker',
      description: '',
      provider: 'none',
      model: null,
      requires_key: false,
      key_vendor: null,
      pricing: '',
      key_label: 'API key',
      eval_tag: '',
    },
    {
      id: 'premium-voyage-2.5',
      tier: 'premium',
      label: 'Voyage rerank-2.5',
      description: '',
      provider: 'voyage',
      model: 'rerank-2.5',
      requires_key: true,
      key_vendor: 'voyage',
      pricing: '',
      key_label: 'Voyage API key',
      eval_tag: '',
    },
  ],
  combos: [],
  quickstart_embedder: 'local-granite',
  quickstart_reranker: 'local-ettin',
};

describe('RAG catalog mapping', () => {
  it('disambiguates same-model embedders by dimensions', () => {
    expect(
      embedderIdForSettings(CATALOG, {
        embedding_provider: 'cohere',
        embedding_model: 'embed-v4.0',
        embedding_dimensions: 1536,
      })
    ).toBe('premium-cohere-1536');
    expect(
      embedderIdForSettings(CATALOG, {
        embedding_provider: 'cohere',
        embedding_model: 'embed-v4.0',
        embedding_dimensions: null,
      })
    ).toBe('premium-cohere');
  });

  it('maps a disabled reranker to the explicit none option', () => {
    expect(
      rerankerIdForSettings(CATALOG, {
        rag_rerank_enabled: false,
        rag_rerank_provider: 'voyage',
        rag_rerank_model: 'rerank-2.5',
      })
    ).toBe('none');
    expect(
      rerankerIdForSettings(CATALOG, {
        rag_rerank_enabled: true,
        rag_rerank_provider: 'voyage',
        rag_rerank_model: 'rerank-2.5',
      })
    ).toBe('premium-voyage-2.5');
  });

  it('carries the embedder endpoint and clears it on providers without one', () => {
    const voyage = buildRagUpdate({
      catalog: CATALOG,
      embedderId: 'premium-voyage-large',
      rerankerId: 'premium-voyage-2.5',
      retrievalMode: 'hybrid',
      embeddingKey: 'vk-1',
      rerankKey: '',
    });
    expect(voyage?.embedding_base_url).toBe('https://api.voyageai.com/v1');
    expect(voyage?.embedding_input_type).toBe('voyage');
    expect(voyage?.rag_rerank_enabled).toBe(true);
    expect(voyage?.embedding_api_key).toBe('vk-1');
    expect(voyage?.rag_rerank_api_key).toBeUndefined();

    const cohere = buildRagUpdate({
      catalog: CATALOG,
      embedderId: 'premium-cohere',
      rerankerId: 'none',
      retrievalMode: 'vector',
      embeddingKey: '',
      rerankKey: '',
    });
    // Explicit nulls so a switch AWAY from Voyage drops the stale endpoint.
    expect(cohere?.embedding_base_url).toBeNull();
    expect(cohere?.embedding_input_type).toBeNull();
    expect(cohere?.rag_rerank_enabled).toBe(false);
    expect(cohere?.rag_retrieval_mode).toBe('vector');
    expect(cohere?.embedding_api_key).toBeUndefined();
  });
});

describe('auth path detection', () => {
  it('classifies saved settings into the three auth paths', () => {
    expect(detectAuthPath({ llm_provider: 'ollama', llm_base_url: null })).toBe('local');
    expect(
      detectAuthPath({ llm_provider: 'anthropic', llm_base_url: 'http://localhost:8318' })
    ).toBe('cliproxy');
    expect(detectAuthPath({ llm_provider: 'anthropic', llm_base_url: null })).toBe('api_key');
    expect(detectAuthPath({ llm_provider: 'groq', llm_base_url: null })).toBe('api_key');
  });

  it('resolves the CLIProxy entry from provider, mode, and model', () => {
    const catalog = [
      cliproxyEntry({}),
      cliproxyEntry({
        id: 'codex',
        nymeria_provider: 'openai',
        api_mode: 'responses',
        default_model: 'gpt-5.5',
      }),
      cliproxyEntry({
        id: 'kimi',
        nymeria_provider: 'openai',
        api_mode: 'chat_completions',
        default_model: 'kimi-k3',
      }),
      cliproxyEntry({
        id: 'grok',
        nymeria_provider: 'openai',
        api_mode: 'responses',
        default_model: 'grok-4.3',
      }),
    ];
    expect(
      detectCliproxyEntry(catalog, {
        llm_provider: 'anthropic',
        llm_model: 'claude-opus-4-7',
        openai_api_mode: null,
      })
    ).toBe('claude');
    expect(
      detectCliproxyEntry(catalog, {
        llm_provider: 'openai',
        llm_model: 'gpt-5.5',
        openai_api_mode: 'responses',
      })
    ).toBe('codex');
    expect(
      detectCliproxyEntry(catalog, {
        llm_provider: 'openai',
        llm_model: 'kimi-k3',
        openai_api_mode: 'chat_completions',
      })
    ).toBe('kimi');
    // Grok rides responses since 2026-08-07: the default-model match must
    // beat the old "responses-mode openai = codex" shortcut.
    expect(
      detectCliproxyEntry(catalog, {
        llm_provider: 'openai',
        llm_model: 'grok-4.3',
        openai_api_mode: 'responses',
      })
    ).toBe('grok');
    // Non-default models on the name-scoped CLIs resolve by family.
    expect(
      detectCliproxyEntry(catalog, {
        llm_provider: 'openai',
        llm_model: 'grok-4.5',
        openai_api_mode: 'responses',
      })
    ).toBe('grok');
    expect(
      detectCliproxyEntry(catalog, {
        llm_provider: 'openai',
        llm_model: 'kimi-k2.5',
        openai_api_mode: 'chat_completions',
      })
    ).toBe('kimi');
  });
});

describe('registryProviderGroups', () => {
  it('groups the fetched registry by tier in native, gateway, unverified order', () => {
    const catalog = [
      { id: 'groq', label: 'Groq', tier: 'unverified' },
      { id: 'anthropic', label: 'Anthropic', tier: 'native' },
      { id: 'openrouter', label: 'OpenRouter', tier: 'gateway' },
    ] as unknown as LLMProviderSpec[];
    const groups = registryProviderGroups(catalog);
    expect(groups.map((g) => g.label)).toEqual(['Native reasoning', 'Gateway', 'Unverified']);
    expect(groups[0].options[0].value).toBe('anthropic');
  });

  it('falls back to a minimal static group when the catalog is empty', () => {
    const groups = registryProviderGroups([]);
    expect(groups).toHaveLength(1);
    expect(groups[0].options.map((o) => o.value)).toContain('anthropic');
  });
});
