/**
 * Model options for each LLM provider.
 * Shared between SettingsPanel and AgentForm.
 */

import type { LLMProvider } from '$lib/types';

export interface ModelOption {
  value: string;
  label: string;
}

export const modelOptions: Record<LLMProvider, ModelOption[]> = {
  anthropic: [
    { value: 'claude-opus-4-20250514', label: 'claude-opus-4 (best)' },
    { value: 'claude-sonnet-4-20250514', label: 'claude-sonnet-4 (reliable)' },
    { value: 'claude-3-5-haiku-20241022', label: 'claude-3.5-haiku (fast-cheap)' }
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
