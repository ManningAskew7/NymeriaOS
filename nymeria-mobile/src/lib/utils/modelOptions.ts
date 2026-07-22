/**
 * Static OFFLINE fallback model suggestions for a few common providers.
 *
 * This is NOT the provider list. Nymeria supports the full backend registry
 * (130+ providers, `Nymeria/nymeria/config/llm_providers.py`, served by
 * `GET /settings/llm/providers`), and the authoritative model list for any
 * provider is the live model endpoint (`getAvailableModels`). These presets
 * only seed dropdowns when the live list is unavailable; providers absent
 * here fall back to the registry spec's `default_model` plus free-text entry.
 */

export interface ModelOption {
  value: string;
  label: string;
}

export const modelOptions: Record<string, ModelOption[]> = {
  anthropic: [
    { value: 'claude-opus-4-8', label: 'claude-opus-4-8 (best)' },
    { value: 'claude-sonnet-4-6', label: 'claude-sonnet-4-6 (reliable)' },
    { value: 'claude-haiku-4-5', label: 'claude-haiku-4-5 (fast-cheap)' }
  ],
  openai: [
    { value: 'gpt-5.2', label: 'gpt-5.2 (untested)' },
    { value: 'gpt-5.2-codex', label: 'gpt-5.2-codex (untested)' }
  ],
  openrouter: [
    { value: 'qwen/qwen3-coder-next', label: 'qwen/qwen3-coder-next (cheap-capable)' },
    { value: 'anthropic/claude-opus-4.6', label: 'anthropic/claude-opus-4.6 (best)' },
    { value: 'anthropic/claude-sonnet-4.5', label: 'anthropic/claude-sonnet-4.5 (reliable)' },
    { value: 'moonshotai/kimi-k2.5', label: 'moonshotai/kimi-k2.5 (budget-excellent)' },
    { value: 'deepseek/deepseek-v3.2', label: 'deepseek/deepseek-v3.2 (smart-slow-cheap)' },
    { value: 'z-ai/glm-4.7', label: 'z-ai/glm-4.7 (value-experimental)' },
    { value: 'z-ai/glm-4.7-flash', label: 'z-ai/glm-4.7-flash (cheap-fast-experimental)' },
    { value: 'openai/gpt-5.2', label: 'openai/gpt-5.2 (untested)' },
    { value: 'openai/gpt-5.2-codex', label: 'openai/gpt-5.2-codex (untested)' }
  ]
};
