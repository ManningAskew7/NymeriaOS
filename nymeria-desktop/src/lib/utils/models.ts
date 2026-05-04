import { api } from '$lib/services/api.svelte';
import type { AvailableModel } from '$lib/types';

export interface AvailableModelsState {
  models: AvailableModel[];
  provider: string;
  loading: boolean;
}

export function providerHasAvailableModels(provider: string): boolean {
  return provider === 'anthropic' || provider === 'openai';
}

export async function resolveAvailableModels(
  provider: string,
  current: Pick<AvailableModelsState, 'models' | 'provider'>
): Promise<Pick<AvailableModelsState, 'models' | 'provider'>> {
  if (!providerHasAvailableModels(provider)) {
    return { models: [], provider: '' };
  }
  if (current.provider === provider && current.models.length > 0) {
    return current;
  }
  try {
    return {
      models: await api.getAvailableModels(provider),
      provider,
    };
  } catch {
    return { models: [], provider: '' };
  }
}

export function clearAvailableModels(state: AvailableModelsState): void {
  state.models = [];
  state.provider = '';
  state.loading = false;
}

export async function loadAvailableModels(
  provider: string,
  state: AvailableModelsState
): Promise<void> {
  if (!providerHasAvailableModels(provider)) {
    clearAvailableModels(state);
    state.loading = false;
    return;
  }
  if (state.provider === provider && state.models.length > 0) {
    state.loading = false;
    return;
  }
  state.loading = true;
  const next = await resolveAvailableModels(provider, state);
  state.models = next.models;
  state.provider = next.provider;
  state.loading = false;
}
