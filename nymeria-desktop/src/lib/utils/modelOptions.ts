/**
 * Model options for each LLM provider.
 * Shared between SettingsPanel and AgentForm.
 */

import type { LLMProvider } from '$lib/types';

export const modelOptions: Record<LLMProvider, string[]> = {
  anthropic: [
    'claude-sonnet-4-20250514',
    'claude-opus-4-20250514',
    'claude-3-5-sonnet-20241022',
    'claude-3-5-haiku-20241022'
  ],
  openai: ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],
  openrouter: [
    'anthropic/claude-sonnet-4.5',
    'anthropic/claude-3.5-sonnet',
    'openai/gpt-4o',
    'google/gemini-pro-1.5'
  ]
};
