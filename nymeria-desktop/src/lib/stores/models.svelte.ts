/**
 * Lazy-loading store for OpenRouter model metadata.
 * Fetches once per session (no polling).
 */

import type { ModelMetadata } from '$lib/types';
import { api } from '$lib/services/api.svelte';

function createModelsStore() {
  let models = $state<ModelMetadata[]>([]);
  let modelMap = $state<Map<string, ModelMetadata>>(new Map());
  let loaded = $state(false);
  let loading = $state(false);

  async function loadModels() {
    if (loaded || loading) return;
    loading = true;
    try {
      const result = await api.getOpenRouterModels();
      if (result.length > 0) {
        models = result;
        const map = new Map<string, ModelMetadata>();
        for (const m of result) {
          map.set(m.id.toLowerCase(), m);
        }
        modelMap = map;
        loaded = true;
      }
      // Empty result = cache not populated yet, allow retry
    } catch {
      // Non-critical — frontend works without metadata
    } finally {
      loading = false;
    }
  }

  function getById(modelId: string): ModelMetadata | undefined {
    if (!modelId) return undefined;
    const key = modelId.toLowerCase();

    // Direct match
    const direct = modelMap.get(key);
    if (direct) return direct;

    // Prefix match (e.g. "anthropic/claude-sonnet-4" matches "anthropic/claude-sonnet-4:beta")
    for (const [cachedId, info] of modelMap) {
      if (key.startsWith(cachedId) || cachedId.startsWith(key)) {
        return info;
      }
    }
    return undefined;
  }

  function formatPrice(pricePerToken: number | null): string {
    if (pricePerToken == null || pricePerToken <= 0) return '—';
    const perMillion = pricePerToken * 1_000_000;
    if (perMillion < 0.01) return '<$0.01/M';
    return `$${perMillion.toFixed(2)}/M`;
  }

  function formatContext(contextLength: number): string {
    if (contextLength >= 1_000_000) return `${(contextLength / 1_000_000).toFixed(1)}M`;
    if (contextLength >= 1_000) return `${Math.round(contextLength / 1_000)}K`;
    return String(contextLength);
  }

  return {
    get models() { return models; },
    get loaded() { return loaded; },
    get loading() { return loading; },
    loadModels,
    getById,
    formatPrice,
    formatContext,
  };
}

export const modelsStore = createModelsStore();
