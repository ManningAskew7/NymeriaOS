import type { LLMProvider } from '$lib/types';

export type ProviderOption = { value: string; label: string };
export type SettingsDisplayProvider = string;
export type ThreadDisplayProvider = string;

export const HOSTED_OPENAI_COMPATIBLE_PROVIDER_OPTIONS: ProviderOption[] = [
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'azure-foundry', label: 'Azure AI Foundry' },
  { value: 'xai', label: 'xAI' },
  { value: 'google', label: 'Google Gemini' },
  { value: 'google-vertex', label: 'Google Vertex AI' },
  { value: 'groq', label: 'Groq' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'mistral', label: 'Mistral AI' },
  { value: 'cohere', label: 'Cohere' },
  { value: 'togetherai', label: 'Together AI' },
  { value: 'fireworks-ai', label: 'Fireworks AI' },
  { value: 'perplexity', label: 'Perplexity' },
  { value: 'cerebras', label: 'Cerebras' },
  { value: 'sambanova', label: 'SambaNova' },
  { value: 'nvidia', label: 'NVIDIA NIM' },
  { value: 'huggingface', label: 'Hugging Face' },
  { value: 'deepinfra', label: 'DeepInfra' },
  { value: 'moonshotai', label: 'Moonshot / Kimi' },
  { value: 'aihubmix', label: 'AIHubMix' },
  { value: 'alibaba', label: 'Alibaba / Qwen' },
  { value: 'alibaba-coding-plan', label: 'Alibaba Coding Plan' },
  { value: 'qwen-oauth', label: 'Qwen Portal' },
  { value: 'zai', label: 'Z.ai' },
  { value: 'zhipuai', label: 'Zhipu AI' },
  { value: 'qianfan', label: 'Baidu Qianfan' },
  { value: 'stepfun', label: 'StepFun' },
  { value: 'volcengine', label: 'Volcengine Ark' },
  { value: 'volcengine-coding-plan', label: 'Volcengine Coding Plan' },
  { value: 'byteplus', label: 'BytePlus ModelArk' },
  { value: 'vercel', label: 'Vercel AI Gateway' },
  { value: 'v0', label: 'Vercel v0' },
  { value: 'github-models', label: 'GitHub Models' },
  { value: 'github-copilot', label: 'GitHub Copilot' },
  { value: 'requesty', label: 'Requesty' },
  { value: 'poe', label: 'Poe' },
  { value: 'gmi', label: 'GMI Cloud' },
  { value: 'nous', label: 'Nous Research' },
  { value: 'tencent-tokenhub', label: 'Tencent TokenHub' },
  { value: 'novita-ai', label: 'Novita AI' },
  { value: 'siliconflow', label: 'SiliconFlow' },
  { value: 'arcee', label: 'Arcee AI' },
  { value: 'chutes', label: 'Chutes' },
  { value: 'venice', label: 'Venice AI' },
  { value: 'kilocode', label: 'Kilo Code Gateway' },
  { value: 'ollama-cloud', label: 'Ollama Cloud' },
];

export const LOCAL_OPENAI_COMPATIBLE_PROVIDER_OPTIONS: ProviderOption[] = [
  { value: 'ollama', label: 'Ollama local' },
  { value: 'lmstudio', label: 'LM Studio' },
  { value: 'llamacpp', label: 'llama.cpp server' },
  { value: 'vllm', label: 'vLLM' },
  { value: 'localai', label: 'LocalAI' },
  { value: 'litellm', label: 'LiteLLM proxy' },
  { value: 'tgi', label: 'Hugging Face TGI' },
];

export const LOCAL_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', 'host.docker.internal'];
export const DEFAULT_LOCAL_BASE_URL = 'http://host.docker.internal:8080/v1';
export const DEFAULT_CLIPROXY_BASE_URL = 'http://cli-proxy-api:8317';
export const DEFAULT_OPENAI_CLIPROXY_BASE_URL = 'http://cli-proxy-api-latest:8317/v1';
export const DEFAULT_CUSTOM_OPENAI_BASE_URL = DEFAULT_OPENAI_CLIPROXY_BASE_URL;

const MANAGED_BASE_URLS = [
  DEFAULT_CLIPROXY_BASE_URL,
  DEFAULT_OPENAI_CLIPROXY_BASE_URL,
  DEFAULT_LOCAL_BASE_URL,
];

export function isLocalBaseUrl(baseUrl: string | null | undefined): boolean {
  if (!baseUrl) return false;
  return LOCAL_HOSTS.some((host) => baseUrl.includes(host));
}

export function normalizeBaseUrl(baseUrl: string | null | undefined): string {
  return (baseUrl || '').trim().replace(/\/+$/, '');
}

export function isManagedBaseUrl(baseUrl: string | null | undefined): boolean {
  const normalized = normalizeBaseUrl(baseUrl);
  return !!normalized && MANAGED_BASE_URLS.some((url) => normalizeBaseUrl(url) === normalized);
}

export function toSettingsDisplayProvider(
  provider: LLMProvider,
  baseUrl: string | null | undefined
): SettingsDisplayProvider {
  if (provider === 'anthropic' && !baseUrl) return 'anthropic_direct';
  if (provider === 'anthropic') return 'anthropic_proxy';
  if (provider === 'openai' && isLocalBaseUrl(baseUrl)) return 'local_openai';
  if (provider === 'openai' && baseUrl) return 'openai_custom';
  return provider as SettingsDisplayProvider;
}

export function fromSettingsDisplayProvider(
  displayProvider: SettingsDisplayProvider
): { provider: LLMProvider; clearBaseUrl: boolean } {
  if (displayProvider === 'anthropic_proxy') return { provider: 'anthropic', clearBaseUrl: false };
  if (displayProvider === 'anthropic_direct') return { provider: 'anthropic', clearBaseUrl: true };
  if (displayProvider === 'local_openai') return { provider: 'openai', clearBaseUrl: false };
  if (displayProvider === 'openai_custom') return { provider: 'openai', clearBaseUrl: false };
  if (displayProvider === 'openai') return { provider: 'openai', clearBaseUrl: true };
  return { provider: displayProvider as LLMProvider, clearBaseUrl: false };
}

export function toThreadDisplayProvider(
  provider: string,
  baseUrl?: string | null
): ThreadDisplayProvider {
  if (!provider) return '';
  if (provider === 'anthropic' && baseUrl === '') return 'anthropic_direct';
  if (provider === 'anthropic') return 'anthropic_proxy';
  if (provider === 'openai' && baseUrl) return 'openai_custom';
  return provider as ThreadDisplayProvider;
}

export function fromThreadDisplayProvider(
  displayProvider: ThreadDisplayProvider
): { provider: string; baseUrl: string | null } {
  if (displayProvider === '') return { provider: '', baseUrl: null };
  if (displayProvider === 'anthropic_proxy') return { provider: 'anthropic', baseUrl: null };
  if (displayProvider === 'anthropic_direct') return { provider: 'anthropic', baseUrl: '' };
  if (displayProvider === 'openai_custom') {
    return { provider: 'openai', baseUrl: DEFAULT_CUSTOM_OPENAI_BASE_URL };
  }
  return { provider: displayProvider, baseUrl: null };
}

export function supportsOpenAiApiMode(provider: string): boolean {
  return !!provider && provider !== 'anthropic' && provider !== 'anthropic_proxy' && provider !== 'anthropic_direct';
}
