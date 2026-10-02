/**
 * Lazy-loading store for OpenRouter model metadata.
 * Fetches once per connection (no polling). A non-empty catalog latches for
 * the connection; an empty or failed load is retried at most once per
 * retry window (`utils/retryWindow.ts`).
 */

import type { AvailableModel, ModelMetadata } from '$lib/types';
import { api } from '$lib/services/api.svelte';
import { insideRetryWindow } from '$lib/utils/retryWindow';
import { registerIdentityReloadHook } from './config.svelte';

// Exported for the store's tests only: each instance registers an identity
// reload hook for life, so the app uses the single `modelsStore`.
export function createModelsStore() {
  let models = $state<ModelMetadata[]>([]);
  let modelMap = $state<Map<string, ModelMetadata>>(new Map());
  let loaded = $state(false);
  let loading = $state(false);
  // When the last load came back empty or failed; plain (not $state) so the
  // no-op inside the window reads and writes nothing an effect could track.
  let emptyAt: number | null = null;
  let identityGeneration = 0;

  // The catalog (and the per-thread available models merged into it) is
  // what the CONNECTED backend serves: drop it on every connection switch
  // so effort clamps and context hints never describe the previous one's
  // models (#242). The new backend is asked at once (#445).
  registerIdentityReloadHook(() => {
    identityGeneration += 1;
    models = [];
    modelMap = new Map();
    loaded = false;
    loading = false;
    emptyAt = null;
  });

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
    // Without the window every panel effect that reads `loaded` or `loading`
    // re-asked on each settle, as fast as the backend answered (#445).
    if (insideRetryWindow(emptyAt)) return;
    const generation = identityGeneration;
    loading = true;
    try {
      const result = await api.getOpenRouterModels();
      if (generation !== identityGeneration) return;
      if (result.length > 0) {
        mergeModelMetadata(result);
        loaded = true;
        emptyAt = null;
      } else {
        // The catalog is not populated (or the request failed: the api
        // client folds every failure into []). Retry after the window.
        emptyAt = Date.now();
      }
    } catch {
      // Non-critical: the frontend works without metadata.
      if (generation === identityGeneration) emptyAt = Date.now();
    } finally {
      if (generation === identityGeneration) loading = false;
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
