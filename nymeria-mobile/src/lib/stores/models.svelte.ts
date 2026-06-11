/**
 * Lazy-loading store for OpenRouter model metadata.
 * Fetches once per session (no polling).
 */

import type { AvailableModel, ModelMetadata } from '$lib/types';
import { api } from '$lib/services/api.svelte';

function createModelsStore() {
  let models = $state<ModelMetadata[]>([]);
  let modelMap = $state<Map<string, ModelMetadata>>(new Map());
  let loaded = $state(false);
  let loading = $state(false);

  function toMetadata(
    model: AvailableModel | ModelMetadata,
    existing?: ModelMetadata
  ): ModelMetadata {
    return {
      id: model.id,
      name: model.name || existing?.name || model.id,
      context_length: model.context_length ?? existing?.context_length ?? 0,
      max_completion_tokens: model.max_completion_tokens ?? existing?.max_completion_tokens ?? null,
      pricing_prompt: model.pricing_prompt ?? existing?.pricing_prompt ?? null,
      pricing_completion: model.pricing_completion ?? existing?.pricing_completion ?? null,
      supported_parameters: model.supported_parameters ?? existing?.supported_parameters ?? [],
      input_modalities: model.input_modalities ?? existing?.input_modalities ?? [],
      tokenizer: model.tokenizer ?? existing?.tokenizer ?? null,
      default_temperature: model.default_temperature ?? existing?.default_temperature ?? null,
      default_top_p: model.default_top_p ?? existing?.default_top_p ?? null,
      default_frequency_penalty: model.default_frequency_penalty ?? existing?.default_frequency_penalty ?? null,
      supported_reasoning_efforts: model.supported_reasoning_efforts ?? existing?.supported_reasoning_efforts,
      max_reasoning_effort: model.max_reasoning_effort ?? existing?.max_reasoning_effort,
    };
  }

  function mergeModelMetadata(nextModels: (AvailableModel | ModelMetadata)[]) {
    if (nextModels.length === 0) return;

    const nextMap = new Map(modelMap);
    const nextList = new Map(models.map((model) => [model.id.toLowerCase(), model]));

    for (const model of nextModels) {
      if (!model.id) continue;
      const key = model.id.toLowerCase();
      const metadata = toMetadata(model, nextMap.get(key));
      nextMap.set(key, metadata);
      nextList.set(key, metadata);
    }

    modelMap = nextMap;
    models = Array.from(nextList.values());
  }

  async function loadModels() {
    if (loaded || loading) return;
    loading = true;
    try {
      const result = await api.getOpenRouterModels();
      if (result.length > 0) {
        mergeModelMetadata(result);
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

  function mergeAvailableModels(availableModels: AvailableModel[]) {
    mergeModelMetadata(availableModels);
  }

  function formatContext(contextLength: number | null | undefined): string {
    if (contextLength == null || contextLength <= 0) return 'unknown';
    if (contextLength >= 1_000_000) return `${(contextLength / 1_000_000).toFixed(1)}M`;
    if (contextLength >= 1_000) return `${Math.round(contextLength / 1_000)}K`;
    return String(contextLength);
  }

  return {
    get models() { return models; },
    get loaded() { return loaded; },
    get loading() { return loading; },
    loadModels,
    mergeAvailableModels,
    getById,
    formatPrice,
    formatContext,
  };
}

export const modelsStore = createModelsStore();
