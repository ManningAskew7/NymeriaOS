<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { api, probeConnection } from '$lib/services/api.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import type {
    ServerSettings,
    LLMProvider,
    OpenAIApiMode,
    LogLevel,
    ThemeName,
    SavedConnection,
    LLMProviderSpec,
    ProviderRoute,
  } from '$lib/types';
  import { getThemeList, getThemePreviewColors } from '$lib/themes';
  import { modelOptions } from '$lib/utils/modelOptions';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';
  import { CredentialManagerPanel } from '../credentials';
  import { MCPManagementPanel, ToolManagementPanel } from '../tools';
  import SkillsPanel from '../skills/SkillsPanel.svelte';
  import NotificationsPanel from '../notifications/NotificationsPanel.svelte';
  import CLIProxyPanel from './CLIProxyPanel.svelte';
  import SystemPromptEditor from './SystemPromptEditor.svelte';
  import GlobalMemoryEditor from './GlobalMemoryEditor.svelte';
  import ProviderSetupWizard from './ProviderSetupWizard.svelte';
  import ProviderSelect from './ProviderSelect.svelte';
  import { AccountTab, UsersTab } from '../account';
  import { backendProcessStore } from '$lib/stores/backendProcess.svelte';
  import { loadAvailableModels, type AvailableModelsState } from '$lib/utils/models';
  import {
    DEFAULT_CLIPROXY_BASE_URL,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_OPENAI_CLIPROXY_BASE_URL,
    buildProviderGroups,
    fromSettingsDisplayProvider as fromDisplayProvider,
    isLocalBaseUrl,
    isManagedBaseUrl,
    toSettingsDisplayProvider as toDisplayProvider,
  } from '$lib/utils/providerMapping';
  import type { SettingsDisplayProvider as DisplayProvider } from '$lib/utils/providerMapping';
  import {
    coerceProviderRoute,
    hasRouteChoice,
    providerRouteLabel,
    supportedRoutesForProvider,
  } from '$lib/utils/providerRoutes';

  interface Props {
    initialTab?: string;
  }

  let { initialTab }: Props = $props();

  // Connection settings
  let apiUrl = $state(configStore.apiUrl);
  let apiKey = $state(configStore.apiKey);

  // Saved connections UI state
  let showSaveInput = $state(false);
  let saveConnectionName = $state('');
  let editingConnectionId = $state<string | null>(null);
  let editingName = $state('');

  function shouldReplaceBaseUrlForProvider(): boolean {
    return !llmBaseUrl || isManagedBaseUrl(llmBaseUrl);
  }

  // Server settings
  let serverSettings = $state<ServerSettings | null>(null);
  let displayProvider = $state<DisplayProvider>('anthropic_proxy');
  let llmProvider = $state<LLMProvider>('anthropic');
  let llmProviderRoute = $state<ProviderRoute | null>(null);
  let llmModel = $state('claude-sonnet-4-20250514');
  let llmTemperature = $state(1);
  let showModelHelp = $state(false);
  // Advanced LLM settings
  let llmMaxTokens = $state<number | null>(null);
  let llmTopP = $state<number | null>(null);
  let llmTopK = $state<number | null>(null);
  let llmFrequencyPenalty = $state<number | null>(null);
  let llmPresencePenalty = $state<number | null>(null);
  let llmReasoningEffort = $state<string | null>(null);
  let llmExtendedThinking = $state(false);
  let dynamicToolBinding = $state(false);
  let llmUseModelDefaults = $state(false);
  let llmBaseUrl = $state('');
  let llmContextLength = $state<number | null | undefined>(null);
  let llmOllamaNumCtx = $state<number | null | undefined>(null);
  let openaiApiMode = $state<OpenAIApiMode>('responses');
  let showAdvancedLlm = $state(false);
  // Agent settings
  let contextManagement = $state<string>('auto_compact');
  let compactThreshold = $state(0.8);
  let compactThresholdMode = $state<'percentage' | 'tokens'>('percentage');
  let compactThresholdTokens = $state(100000);
  let slidingWindowCycles = $state(5);
  let memoryCharLimit = $state(8000);
  let logLevel = $state<LogLevel>('INFO');
  let watchdogEnabled = $state(true);
  let watchdogIntervalMinutes = $state(5);
  let todoStalenessMinutes = $state(20);

  // Voice settings
  let ttsProvider = $state<string>('none');
  let ttsBaseUrl = $state('');
  let ttsModel = $state('tts-1-hd');
  let ttsVoice = $state('nova');
  let ttsOutputFormat = $state('mp3');
  let ttsSpeed = $state(1.0);
  let sttProvider = $state<string>('none');
  let sttBaseUrl = $state('');
  let sttModel = $state('gpt-4o-mini-transcribe');
  let sttLanguage = $state('');
  let voiceDefaultThreadId = $state('');

  let providerCatalog = $state<LLMProviderSpec[]>([]);
  // Tier-grouped picker options. Built reactively from the catalog so backend
  // tier changes (or notes_for_user updates) flow through without a redeploy.
  // Falls back to an unverified-only grouping until the catalog loads.
  let settingsProviderGroups = $derived(buildProviderGroups(providerCatalog));

  // Theme settings
  let selectedTheme = $state<ThemeName>(configStore.theme);
  const themeList = getThemeList();

  // Font picker (testing-only, persisted in localStorage)
  const FONT_STORAGE_KEY = 'nymeria_font_family';
  const SYSTEM_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif";
  const fontOptions = [
    { id: 'geist', name: 'Geist', description: 'Current — clean, modern (Vercel)', stack: `'Geist', ${SYSTEM_STACK}` },
    { id: 'system', name: 'System default', description: 'Original — Segoe UI on Windows', stack: SYSTEM_STACK },
    { id: 'inter', name: 'Inter', description: 'Crisp, neutral (Linear, Notion)', stack: `'Inter', ${SYSTEM_STACK}` },
    { id: 'outfit', name: 'Outfit', description: 'Friendly geometric sans', stack: `'Outfit', ${SYSTEM_STACK}` },
    { id: 'dm-sans', name: 'DM Sans', description: 'Soft modern grotesque', stack: `'DM Sans', ${SYSTEM_STACK}` },
    { id: 'jakarta', name: 'Plus Jakarta Sans', description: 'Slightly condensed, modern', stack: `'Plus Jakarta Sans', ${SYSTEM_STACK}` },
    { id: 'manrope', name: 'Manrope', description: 'Rounded, approachable', stack: `'Manrope', ${SYSTEM_STACK}` },
  ];

  function detectInitialFontId(): string {
    if (typeof localStorage === 'undefined') return 'geist';
    const stored = localStorage.getItem(FONT_STORAGE_KEY);
    if (!stored) return 'geist';
    const match = fontOptions.find((opt) => opt.stack === stored);
    return match?.id ?? 'geist';
  }

  let selectedFontId = $state(detectInitialFontId());

  function handleFontChange(id: string) {
    const opt = fontOptions.find((o) => o.id === id);
    if (!opt) return;
    selectedFontId = id;
    if (typeof document !== 'undefined') {
      if (id === 'geist') {
        // Default — clear the override so the stylesheet's --font-sans applies
        document.documentElement.style.removeProperty('--font-sans');
      } else {
        document.documentElement.style.setProperty('--font-sans', opt.stack);
      }
    }
    if (typeof localStorage !== 'undefined') {
      if (id === 'geist') localStorage.removeItem(FONT_STORAGE_KEY);
      else localStorage.setItem(FONT_STORAGE_KEY, opt.stack);
    }
  }

  // Logo font picker (testing-only, persisted in localStorage). Same pattern
  // as the body font picker above but applies to the .logo element via the
  // --font-logo CSS variable. The default ("system") matches the original
  // --font-logo declaration in app.css.
  const LOGO_FONT_STORAGE_KEY = 'nymeria_logo_font';
  const logoFontOptions = [
    { id: 'system', name: 'System default', description: 'Original — Segoe UI on Windows', stack: SYSTEM_STACK },
    { id: 'ibm-plex-sans', name: 'IBM Plex Sans', description: 'Corporate, technical, polished', stack: `'IBM Plex Sans', ${SYSTEM_STACK}` },
    { id: 'archivo', name: 'Archivo', description: 'Strong professional grotesque', stack: `'Archivo', ${SYSTEM_STACK}` },
    { id: 'albert-sans', name: 'Albert Sans', description: 'Clean modern professional', stack: `'Albert Sans', ${SYSTEM_STACK}` },
    { id: 'space-grotesk', name: 'Space Grotesk', description: 'Modern, slightly geometric', stack: `'Space Grotesk', ${SYSTEM_STACK}` },
    { id: 'sora', name: 'Sora', description: 'Clean geometric, contemporary', stack: `'Sora', ${SYSTEM_STACK}` },
    { id: 'orbitron', name: 'Orbitron', description: 'Futuristic sci-fi OS feel', stack: `'Orbitron', ${SYSTEM_STACK}` },
    { id: 'rajdhani', name: 'Rajdhani', description: 'Narrow, technical', stack: `'Rajdhani', ${SYSTEM_STACK}` },
    { id: 'exo-2', name: 'Exo 2', description: 'Semi-rounded sci-fi', stack: `'Exo 2', ${SYSTEM_STACK}` },
    { id: 'unbounded', name: 'Unbounded', description: 'Distinctive geometric display', stack: `'Unbounded', ${SYSTEM_STACK}` },
  ];

  function detectInitialLogoFontId(): string {
    if (typeof localStorage === 'undefined') return 'system';
    const stored = localStorage.getItem(LOGO_FONT_STORAGE_KEY);
    if (!stored) return 'system';
    const match = logoFontOptions.find((opt) => opt.stack === stored);
    return match?.id ?? 'system';
  }

  let selectedLogoFontId = $state(detectInitialLogoFontId());

  function handleLogoFontChange(id: string) {
    const opt = logoFontOptions.find((o) => o.id === id);
    if (!opt) return;
    selectedLogoFontId = id;
    if (typeof document !== 'undefined') {
      if (id === 'system') {
        // Default — clear the override so the stylesheet's --font-logo applies
        document.documentElement.style.removeProperty('--font-logo');
      } else {
        document.documentElement.style.setProperty('--font-logo', opt.stack);
      }
    }
    if (typeof localStorage !== 'undefined') {
      if (id === 'system') localStorage.removeItem(LOGO_FONT_STORAGE_KEY);
      else localStorage.setItem(LOGO_FONT_STORAGE_KEY, opt.stack);
    }
  }

  // Logo size / weight / opacity steppers (testing-only). Each control writes
  // a CSS custom property on :root and persists to localStorage. The same
  // values are applied by a preload script in app.html so there's no flash.
  const LOGO_SIZE_KEY = 'nymeria_logo_size';
  const LOGO_WEIGHT_NAME_KEY = 'nymeria_logo_weight_name';
  const LOGO_WEIGHT_OS_KEY = 'nymeria_logo_weight_os';
  const LOGO_OPACITY_NAME_KEY = 'nymeria_logo_opacity_name';
  const LOGO_OPACITY_OS_KEY = 'nymeria_logo_opacity_os';

  const LOGO_SIZE_DEFAULT = 20; // matches --font-size-xl (1.25rem)
  const LOGO_SIZE_MIN = 12;
  const LOGO_SIZE_MAX = 36;

  const LOGO_WEIGHT_NAME_DEFAULT = 700;
  const LOGO_WEIGHT_OS_DEFAULT = 300;
  const LOGO_WEIGHT_STEPS = [300, 400, 500, 600, 700];

  const LOGO_OPACITY_DEFAULT = 100; // percent
  const LOGO_OPACITY_MIN = 10;
  const LOGO_OPACITY_MAX = 100;

  function readNum(key: string, fallback: number): number {
    if (typeof localStorage === 'undefined') return fallback;
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    const n = Number(raw);
    return Number.isFinite(n) ? n : fallback;
  }

  let logoSize = $state(readNum(LOGO_SIZE_KEY, LOGO_SIZE_DEFAULT));
  let logoWeightName = $state(readNum(LOGO_WEIGHT_NAME_KEY, LOGO_WEIGHT_NAME_DEFAULT));
  let logoWeightOs = $state(readNum(LOGO_WEIGHT_OS_KEY, LOGO_WEIGHT_OS_DEFAULT));
  let logoOpacityName = $state(readNum(LOGO_OPACITY_NAME_KEY, LOGO_OPACITY_DEFAULT));
  let logoOpacityOs = $state(readNum(LOGO_OPACITY_OS_KEY, LOGO_OPACITY_DEFAULT));

  // Logo color — three-way pick between the theme accent (default), white, or
  // black. Writes --logo-color on :root; the live logo and the preview cards
  // both inherit from this variable.
  const LOGO_COLOR_KEY = 'nymeria_logo_color';
  type LogoColor = 'accent' | 'white' | 'black';
  const LOGO_COLOR_DEFAULT: LogoColor = 'accent';

  function detectInitialLogoColor(): LogoColor {
    if (typeof localStorage === 'undefined') return LOGO_COLOR_DEFAULT;
    const stored = localStorage.getItem(LOGO_COLOR_KEY);
    if (stored === 'white' || stored === 'black') return stored;
    return LOGO_COLOR_DEFAULT;
  }

  let logoColor = $state<LogoColor>(detectInitialLogoColor());

  function setLogoColor(color: LogoColor) {
    logoColor = color;
    if (color === 'accent') {
      applyOrClear('--logo-color', null);
      persistOrClear(LOGO_COLOR_KEY, null);
    } else {
      applyOrClear('--logo-color', color === 'white' ? '#ffffff' : '#000000');
      persistOrClear(LOGO_COLOR_KEY, color);
    }
  }

  function resetLogoColor() { setLogoColor(LOGO_COLOR_DEFAULT); }

  function applyOrClear(cssVar: string, value: string | null) {
    if (typeof document === 'undefined') return;
    if (value === null) document.documentElement.style.removeProperty(cssVar);
    else document.documentElement.style.setProperty(cssVar, value);
  }

  function persistOrClear(key: string, value: string | null) {
    if (typeof localStorage === 'undefined') return;
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  }

  function setLogoSize(next: number) {
    const clamped = Math.max(LOGO_SIZE_MIN, Math.min(LOGO_SIZE_MAX, Math.round(next)));
    logoSize = clamped;
    if (clamped === LOGO_SIZE_DEFAULT) {
      applyOrClear('--logo-font-size', null);
      persistOrClear(LOGO_SIZE_KEY, null);
    } else {
      applyOrClear('--logo-font-size', `${clamped}px`);
      persistOrClear(LOGO_SIZE_KEY, String(clamped));
    }
  }

  function stepLogoWeight(target: 'name' | 'os', delta: 1 | -1) {
    const current = target === 'name' ? logoWeightName : logoWeightOs;
    const idx = LOGO_WEIGHT_STEPS.indexOf(current);
    // If the current value isn't on the step ladder, snap to nearest then step.
    const baseIdx = idx >= 0
      ? idx
      : LOGO_WEIGHT_STEPS.findIndex((w) => w >= current);
    const safeIdx = baseIdx < 0 ? LOGO_WEIGHT_STEPS.length - 1 : baseIdx;
    const nextIdx = Math.max(0, Math.min(LOGO_WEIGHT_STEPS.length - 1, safeIdx + delta));
    const next = LOGO_WEIGHT_STEPS[nextIdx];
    setLogoWeight(target, next);
  }

  function setLogoWeight(target: 'name' | 'os', next: number) {
    if (target === 'name') {
      logoWeightName = next;
      const isDefault = next === LOGO_WEIGHT_NAME_DEFAULT;
      applyOrClear('--logo-weight-name', isDefault ? null : String(next));
      persistOrClear(LOGO_WEIGHT_NAME_KEY, isDefault ? null : String(next));
    } else {
      logoWeightOs = next;
      const isDefault = next === LOGO_WEIGHT_OS_DEFAULT;
      applyOrClear('--logo-weight-os', isDefault ? null : String(next));
      persistOrClear(LOGO_WEIGHT_OS_KEY, isDefault ? null : String(next));
    }
  }

  function setLogoOpacity(target: 'name' | 'os', nextPercent: number) {
    const clamped = Math.max(LOGO_OPACITY_MIN, Math.min(LOGO_OPACITY_MAX, Math.round(nextPercent)));
    const isDefault = clamped === LOGO_OPACITY_DEFAULT;
    const cssVar = target === 'name' ? '--logo-opacity-name' : '--logo-opacity-os';
    const key = target === 'name' ? LOGO_OPACITY_NAME_KEY : LOGO_OPACITY_OS_KEY;
    if (target === 'name') logoOpacityName = clamped;
    else logoOpacityOs = clamped;
    applyOrClear(cssVar, isDefault ? null : (clamped / 100).toString());
    persistOrClear(key, isDefault ? null : String(clamped));
  }

  function resetLogoSize() { setLogoSize(LOGO_SIZE_DEFAULT); }
  function resetLogoWeightName() { setLogoWeight('name', LOGO_WEIGHT_NAME_DEFAULT); }
  function resetLogoWeightOs() { setLogoWeight('os', LOGO_WEIGHT_OS_DEFAULT); }
  function resetLogoOpacityName() { setLogoOpacity('name', LOGO_OPACITY_DEFAULT); }
  function resetLogoOpacityOs() { setLogoOpacity('os', LOGO_OPACITY_DEFAULT); }

  function resetAllLogoTuning() {
    resetLogoColor();
    resetLogoSize();
    resetLogoWeightName();
    resetLogoWeightOs();
    resetLogoOpacityName();
    resetLogoOpacityOs();
  }

  // Chat bubble preference (off by default, on = restore the bubble look)
  const CHAT_BUBBLES_KEY = 'nymeria_chat_bubbles';
  function detectInitialBubbles(): boolean {
    if (typeof localStorage === 'undefined') return false;
    return localStorage.getItem(CHAT_BUBBLES_KEY) === 'on';
  }
  let showChatBubbles = $state(detectInitialBubbles());

  function handleChatBubblesChange(on: boolean) {
    showChatBubbles = on;
    if (typeof document !== 'undefined') {
      if (on) document.documentElement.setAttribute('data-chat-bubbles', 'on');
      else document.documentElement.removeAttribute('data-chat-bubbles');
    }
    if (typeof localStorage !== 'undefined') {
      if (on) localStorage.setItem(CHAT_BUBBLES_KEY, 'on');
      else localStorage.removeItem(CHAT_BUBBLES_KEY);
    }
  }

  // Model metadata (reactive lookup based on current model ID)
  const currentModelMeta = $derived(
    llmProvider === 'openrouter' ? modelsStore.getById(llmModel) : undefined
  );

  // Dynamic model list for providers that support /v1/models
  let availableModelsState = $state<AvailableModelsState>({
    models: [],
    provider: '',
    loading: false,
  });

  // Sync llmProvider from displayProvider and fetch models from the provider
  // catalog endpoint where available. Static model options are only fallback.
  $effect(() => {
    const { provider } = fromDisplayProvider(displayProvider);
    llmProvider = provider;
    const baseUrlOverride = (
      displayProvider === 'local_openai'
      || displayProvider === 'openai_custom'
      || llmBaseUrl
    ) ? llmBaseUrl : '';
    void loadAvailableModels(provider, availableModelsState, baseUrlOverride);
  });

  $effect(() => {
    if (hasRouteChoice(llmProvider, providerCatalog)) {
      llmProviderRoute = coerceProviderRoute(llmProvider, providerCatalog, llmProviderRoute);
    } else {
      llmProviderRoute = null;
    }
  });

  // Auto-populate the base URL field when the user picks Local LLM,
  // unless they already have a local URL in there.
  $effect(() => {
    if (displayProvider === 'local_openai' && (!isLocalBaseUrl(llmBaseUrl) || isManagedBaseUrl(llmBaseUrl))) {
      llmBaseUrl = DEFAULT_LOCAL_BASE_URL;
    }
  });

  // Auto-populate the base URL field when the user picks OpenAI custom.
  // This is the Codex OAuth path through CLIProxy, which requires /v1.
  $effect(() => {
    if (displayProvider === 'openai_custom' && shouldReplaceBaseUrlForProvider()) {
      llmBaseUrl = DEFAULT_OPENAI_CLIPROXY_BASE_URL;
    }
  });

  // Auto-populate the base URL field when the user picks Anthropic (Subscription)
  // and the field is empty or contains another managed default. Without this,
  // switching between Direct/API/OpenAI-custom can save the wrong URL shape.
  $effect(() => {
    if (displayProvider === 'anthropic_proxy' && shouldReplaceBaseUrlForProvider()) {
      llmBaseUrl = DEFAULT_CLIPROXY_BASE_URL;
    }
  });

  // Hosted/direct providers should not retain one of our managed proxy URLs.
  // Custom user-entered URLs are preserved until save, where the provider
  // mapping decides whether to clear them.
  $effect(() => {
    if (displayProvider !== 'anthropic_proxy' && displayProvider !== 'openai_custom' && displayProvider !== 'local_openai' && isManagedBaseUrl(llmBaseUrl)) {
      llmBaseUrl = '';
    }
  });

  // UI state
  type SettingsTab = 'connection' | 'appearance' | 'memory' | 'llm' | 'agent' | 'persona' | 'tools' | 'mcp' | 'credentials' | 'skills' | 'notifications' | 'voice' | 'proxy' | 'account' | 'users';
  const adminServerTabs: SettingsTab[] = ['llm', 'agent', 'persona', 'voice', 'proxy', 'users'];

  function getInitialTab(): SettingsTab {
    return (initialTab as SettingsTab) || 'connection';
  }

  function isAdminServerTab(tab: SettingsTab): boolean {
    return adminServerTabs.includes(tab);
  }

  let activeTab = $state<SettingsTab>(getInitialTab());
  let isAdmin = $derived(configStore.identity?.role === 'admin');
  let connectionAdvancedTouched = $state(false);
  let showConnectionAdvanced = $state(true);
  let testStatus = $state<'idle' | 'testing' | 'success' | 'error'>('idle');
  let testMessage = $state('');
  let loadingSettings = $state(false);
  let savingSettings = $state(false);
  let showProviderSetupWizard = $state(false);

  $effect(() => {
    if (!connectionAdvancedTouched) {
      showConnectionAdvanced = !backendProcessStore.isManagedBackend;
    }
    if (
      (!isAdmin && isAdminServerTab(activeTab))
      || (activeTab === 'proxy' && backendProcessStore.isExternalBackend)
    ) {
      activeTab = 'connection';
    }
  });

  // Load server settings when connected
  async function loadServerSettings() {
    if (!configStore.isConfigured) return;

    loadingSettings = true;
    try {
      const [settings, catalog] = await Promise.all([
        api.getServerSettings(),
        api.getLLMProviderCatalog(),
      ]);
      serverSettings = settings;
      providerCatalog = catalog;
      llmProvider = serverSettings.llm_provider;
      displayProvider = toDisplayProvider(serverSettings.llm_provider, serverSettings.llm_base_url || '');
      llmProviderRoute = serverSettings.llm_provider_route;
      llmModel = serverSettings.llm_model;
      llmTemperature = serverSettings.llm_temperature;
      llmMaxTokens = serverSettings.llm_max_tokens;
      llmTopP = serverSettings.llm_top_p;
      llmTopK = serverSettings.llm_top_k;
      llmFrequencyPenalty = serverSettings.llm_frequency_penalty;
      llmPresencePenalty = serverSettings.llm_presence_penalty;
      llmReasoningEffort = serverSettings.llm_reasoning_effort;
      llmExtendedThinking = serverSettings.llm_extended_thinking;
      dynamicToolBinding = serverSettings.dynamic_tool_binding;
      llmUseModelDefaults = serverSettings.llm_use_model_defaults;
      llmBaseUrl = serverSettings.llm_base_url || '';
      llmContextLength = serverSettings.llm_context_length;
      llmOllamaNumCtx = serverSettings.llm_ollama_num_ctx;
      openaiApiMode = serverSettings.openai_api_mode ?? 'responses';
      contextManagement = serverSettings.context_management;
      compactThreshold = serverSettings.compact_threshold ?? 0.8;
      compactThresholdMode = serverSettings.compact_threshold_mode ?? 'percentage';
      compactThresholdTokens = serverSettings.compact_threshold_tokens ?? 100000;
      // Load model metadata for OpenRouter enrichment
      if (serverSettings.llm_provider === 'openrouter') {
        modelsStore.loadModels();
      }
      slidingWindowCycles = serverSettings.sliding_window_cycles;
      memoryCharLimit = serverSettings.memory_char_limit ?? 8000;
      logLevel = serverSettings.log_level;
      watchdogEnabled = serverSettings.watchdog_enabled;
      watchdogIntervalMinutes = serverSettings.watchdog_interval_minutes;
      todoStalenessMinutes = serverSettings.todo_staleness_minutes;
      // Voice
      ttsProvider = serverSettings.tts_provider ?? 'none';
      ttsBaseUrl = serverSettings.tts_base_url ?? '';
      ttsModel = serverSettings.tts_model ?? 'tts-1-hd';
      ttsVoice = serverSettings.tts_voice ?? 'nova';
      ttsOutputFormat = serverSettings.tts_output_format ?? 'mp3';
      ttsSpeed = serverSettings.tts_speed ?? 1.0;
      sttProvider = serverSettings.stt_provider ?? 'none';
      sttBaseUrl = serverSettings.stt_base_url ?? '';
      sttModel = serverSettings.stt_model ?? 'gpt-4o-mini-transcribe';
      sttLanguage = serverSettings.stt_language ?? '';
      voiceDefaultThreadId = serverSettings.voice_default_thread_id ?? '';
    } catch (e) {
      console.error('Failed to load server settings:', e);
    } finally {
      loadingSettings = false;
    }
  }

  // Load settings on mount if configured
  $effect(() => {
    if (configStore.isConfigured && !serverSettings && !loadingSettings) {
      loadServerSettings();
    }
  });

  // Load model metadata when provider switches to openrouter
  $effect(() => {
    if (displayProvider === 'openrouter') {
      modelsStore.loadModels();
    }
  });

  function showProviderRouteSelect(provider: string = llmProvider): boolean {
    return hasRouteChoice(provider, providerCatalog);
  }

  function showOpenAiApiMode(provider: string = llmProvider): boolean {
    if (!provider || provider === 'anthropic' || provider === 'bedrock') return false;
    if (hasRouteChoice(provider, providerCatalog)) {
      return coerceProviderRoute(provider, providerCatalog, llmProviderRoute) === 'openai_compat';
    }
    return provider !== 'google' && provider !== 'ollama';
  }

  async function handleSaveConnection() {
    // Saving the form is a backend commit: route through applyConnection so the
    // sidebar is cleared and re-synced against the new backend (a bare config
    // write left the previous backend's cached threads in place).
    await connectionsStore.applyConnection(apiUrl, apiKey);
    // Reflect the applied connection back into the form + server settings.
    apiUrl = configStore.apiUrl;
    apiKey = configStore.apiKey;
    serverSettings = null;
    await loadServerSettings();
    testStatus = 'idle';
    testMessage = 'Connection settings saved!';
    setTimeout(() => {
      testMessage = '';
    }, 2000);
  }

  // Saved connections handlers
  function handleSaveCurrentConnection() {
    if (!saveConnectionName.trim()) return;
    connectionsStore.saveCurrentAs(saveConnectionName.trim());
    saveConnectionName = '';
    showSaveInput = false;
  }

  function handleEditConnection(conn: SavedConnection) {
    editingConnectionId = conn.id;
    editingName = conn.name;
    apiUrl = conn.apiUrl;
    apiKey = conn.apiKey;
  }

  async function handleUpdateConnection() {
    if (!editingConnectionId) return;
    const wasActive = connectionsStore.activeConnectionId === editingConnectionId;
    connectionsStore.update(editingConnectionId, {
      name: editingName.trim() || undefined,
      apiUrl,
      apiKey,
    });
    // If editing the active connection and its backend creds actually changed,
    // re-apply so threads clear + resync against the (possibly different)
    // backend instead of leaving the old backend's cached list in the sidebar.
    const norm = (u: string) => u.trim().replace(/\/+$/, '');
    const credsChanged =
      norm(configStore.apiUrl) !== norm(apiUrl) || configStore.apiKey.trim() !== apiKey.trim();
    if (wasActive && credsChanged) {
      await connectionsStore.applyConnection(apiUrl, apiKey);
    }
    editingConnectionId = null;
    editingName = '';
    testMessage = 'Connection updated!';
    setTimeout(() => { testMessage = ''; }, 2000);
  }

  function handleCancelEdit() {
    editingConnectionId = null;
    editingName = '';
    apiUrl = configStore.apiUrl;
    apiKey = configStore.apiKey;
  }

  function handleDeleteConnection(id: string) {
    connectionsStore.delete(id);
  }

  async function handleConnectTo(id: string) {
    await connectionsStore.switchTo(id);
    // Sync local fields to new connection values
    apiUrl = configStore.apiUrl;
    apiKey = configStore.apiKey;
    // Reload server settings after switch
    serverSettings = null;
    await loadServerSettings();
  }

  async function handleTestConnection() {
    testStatus = 'testing';
    testMessage = '';

    // Stateless probe of the entered URL + token. Does NOT touch configStore,
    // so testing never repoints the live app (committing happens on Save).
    const result = await probeConnection(apiUrl, apiKey);
    if (result.ok) {
      testStatus = 'success';
      testMessage = 'Connection successful!';
    } else {
      testStatus = 'error';
      testMessage = result.message;
    }
  }

  async function handleSaveServerSettings() {
    savingSettings = true;
    testMessage = '';

    try {
      const optionalNumberUpdate = (
        value: number | null | undefined,
        original: number | null | undefined
      ): number | null | undefined => value == null ? (original != null ? null : undefined) : value;
      const { provider: actualProvider, clearBaseUrl } = fromDisplayProvider(displayProvider);
      const effectiveBaseUrl = clearBaseUrl
        ? ''
        : displayProvider === 'openai_custom'
          ? (llmBaseUrl || DEFAULT_OPENAI_CLIPROXY_BASE_URL)
          : llmBaseUrl;

      const result = await api.updateServerSettings({
        llm_provider: actualProvider,
        llm_model: llmModel,
        llm_temperature: llmTemperature,
        llm_max_tokens: llmMaxTokens,
        llm_top_p: llmTopP,
        llm_top_k: llmTopK,
        llm_frequency_penalty: llmFrequencyPenalty,
        llm_presence_penalty: llmPresencePenalty,
        llm_reasoning_effort: llmReasoningEffort,
        llm_extended_thinking: llmExtendedThinking,
        dynamic_tool_binding: dynamicToolBinding,
        llm_use_model_defaults: llmUseModelDefaults,
        llm_base_url: effectiveBaseUrl,
        llm_context_length: optionalNumberUpdate(llmContextLength, serverSettings?.llm_context_length),
        llm_ollama_num_ctx: optionalNumberUpdate(llmOllamaNumCtx, serverSettings?.llm_ollama_num_ctx),
        llm_provider_route: showProviderRouteSelect(actualProvider) ? llmProviderRoute : null,
        openai_api_mode: openaiApiMode,
        context_management: contextManagement,
        compact_threshold: compactThreshold,
        compact_threshold_mode: compactThresholdMode,
        compact_threshold_tokens: compactThresholdTokens,
        sliding_window_cycles: slidingWindowCycles,
        memory_char_limit: memoryCharLimit,
        log_level: logLevel,
        watchdog_enabled: watchdogEnabled,
        watchdog_interval_minutes: watchdogIntervalMinutes,
        todo_staleness_minutes: todoStalenessMinutes,
        // Voice
        tts_provider: ttsProvider,
        tts_base_url: ttsBaseUrl || null,
        tts_model: ttsModel,
        tts_voice: ttsVoice,
        tts_output_format: ttsOutputFormat,
        tts_speed: ttsSpeed,
        stt_provider: sttProvider,
        stt_base_url: sttBaseUrl || null,
        stt_model: sttModel,
        stt_language: sttLanguage || null,
        voice_default_thread_id: voiceDefaultThreadId || null,
      });

      testStatus = 'success';
      testMessage = result.restart_required
        ? 'Settings saved! Restart the server for changes to take effect.'
        : 'Settings saved and applied!';
      serverSettingsStore.refresh();
    } catch (e) {
      testStatus = 'error';
      testMessage = e instanceof Error ? e.message : 'Failed to save settings';
    } finally {
      savingSettings = false;
    }
  }

  async function handleProviderSetupSaved() {
    await loadServerSettings();
    serverSettingsStore.refresh();
    testStatus = 'success';
    testMessage = 'Provider setup saved and applied!';
  }

  function handleThemeChange(theme: ThemeName) {
    selectedTheme = theme;
    configStore.setTheme(theme);
  }
</script>

<div class="settings-panel">
  <div class="settings-layout">
    <!-- Left-side navigation, Claude/Perplexity style: grouped sections with a
         label per group and a single column of nav items underneath. -->
    <aside class="settings-sidebar">
      <div class="nav-group">
        <span class="nav-group-label">Account</span>
        <button
          class="nav-item"
          class:active={activeTab === 'account'}
          onclick={() => (activeTab = 'account')}
          type="button"
        >
          <Icon name="user" size={14} />
          <span>Account</span>
        </button>
        <button
          class="nav-item"
          class:active={activeTab === 'connection'}
          onclick={() => (activeTab = 'connection')}
          type="button"
        >
          <Icon name="server" size={14} />
          <span>Backend</span>
        </button>
      </div>

      <div class="nav-group">
        <span class="nav-group-label">Preferences</span>
        <button
          class="nav-item"
          class:active={activeTab === 'appearance'}
          onclick={() => (activeTab = 'appearance')}
          type="button"
        >
          <Icon name="settings" size={14} />
          <span>Appearance</span>
        </button>
        <button
          class="nav-item"
          class:active={activeTab === 'notifications'}
          onclick={() => (activeTab = 'notifications')}
          disabled={!serverSettings}
          type="button"
        >
          <Icon name="bell" size={14} />
          <span>Notifications</span>
        </button>
        <button
          class="nav-item"
          class:active={activeTab === 'memory'}
          onclick={() => (activeTab = 'memory')}
          type="button"
        >
          <Icon name="pin" size={14} />
          <span>Global Memory</span>
        </button>
      </div>

      <div class="nav-group">
        <span class="nav-group-label">Capabilities</span>
        <button
          class="nav-item"
          class:active={activeTab === 'tools'}
          onclick={() => (activeTab = 'tools')}
          disabled={!serverSettings}
          type="button"
        >
          <Icon name="tool" size={14} />
          <span>Tools</span>
        </button>
        <button
          class="nav-item"
          class:active={activeTab === 'mcp'}
          onclick={() => (activeTab = 'mcp')}
          disabled={!serverSettings}
          type="button"
        >
          <Icon name="folder" size={14} />
          <span>MCP Servers</span>
        </button>
        <button
          class="nav-item"
          class:active={activeTab === 'skills'}
          onclick={() => (activeTab = 'skills')}
          disabled={!serverSettings}
          type="button"
        >
          <Icon name="bolt" size={14} />
          <span>Skills</span>
        </button>
        <button
          class="nav-item"
          class:active={activeTab === 'credentials'}
          onclick={() => (activeTab = 'credentials')}
          disabled={!serverSettings}
          type="button"
        >
          <Icon name="cog" size={14} />
          <span>Integrations</span>
        </button>
      </div>

      {#if isAdmin}
        <div class="nav-group">
          <span class="nav-group-label">Server</span>
          <button
            class="nav-item"
            class:active={activeTab === 'llm'}
            onclick={() => (activeTab = 'llm')}
            disabled={!serverSettings}
            type="button"
          >
            <Icon name="terminal" size={14} />
            <span>Model</span>
          </button>
          <button
            class="nav-item"
            class:active={activeTab === 'agent'}
            onclick={() => (activeTab = 'agent')}
            disabled={!serverSettings}
            type="button"
          >
            <Icon name="cog" size={14} />
            <span>Agent</span>
          </button>
          <button
            class="nav-item"
            class:active={activeTab === 'persona'}
            onclick={() => (activeTab = 'persona')}
            disabled={!serverSettings}
            type="button"
          >
            <Icon name="fileText" size={14} />
            <span>System Prompt</span>
          </button>
          <button
            class="nav-item"
            class:active={activeTab === 'voice'}
            onclick={() => (activeTab = 'voice')}
            disabled={!serverSettings}
            type="button"
          >
            <Icon name="chat" size={14} />
            <span>Voice</span>
          </button>
          {#if backendProcessStore.isManagedBackend}
            <button
              class="nav-item"
              class:active={activeTab === 'proxy'}
              onclick={() => (activeTab = 'proxy')}
              type="button"
            >
              <Icon name="server" size={14} />
              <span>CLI Proxy</span>
            </button>
          {/if}
          <button
            class="nav-item"
            class:active={activeTab === 'users'}
            onclick={() => (activeTab = 'users')}
            type="button"
          >
            <Icon name="users" size={14} />
            <span>Users</span>
          </button>
        </div>
      {/if}
    </aside>

    <main class="settings-content">
      {#key activeTab}
      <div class="tab-fade">

  <!-- Connection Tab -->
  {#if activeTab === 'connection'}
    <div class="tab-content">
      <!-- Saved Connections -->
      <div class="saved-connections">
        <div class="section-header">
          <span class="section-title">Saved Connections</span>
        </div>

        {#if connectionsStore.connections.length === 0}
          <p class="hint" style="margin-bottom: var(--spacing-md);">
            No saved connections. Save your current connection to quickly switch between backends.
          </p>
        {:else}
          <div class="connections-list">
            {#each connectionsStore.connections as conn}
              <div class="connection-row" class:active={connectionsStore.activeConnectionId === conn.id}>
                <span class="conn-status-dot" class:connected={connectionsStore.activeConnectionId === conn.id}></span>
                {#if editingConnectionId === conn.id}
                  <input
                    class="conn-name-input"
                    type="text"
                    bind:value={editingName}
                    placeholder="Connection name"
                  />
                {:else}
                  <div class="conn-info">
                    <span class="conn-name">{conn.name}</span>
                    <span class="conn-url">{conn.apiUrl}</span>
                  </div>
                {/if}
                <div class="conn-actions">
                  {#if editingConnectionId === conn.id}
                    <button class="conn-action-btn" onclick={handleUpdateConnection} title="Save">
                      <Icon name="check" size={14} />
                    </button>
                    <button class="conn-action-btn" onclick={handleCancelEdit} title="Cancel">
                      <Icon name="x" size={14} />
                    </button>
                  {:else}
                    {#if connectionsStore.activeConnectionId !== conn.id}
                      <Button
                        variant="secondary"
                        size="sm"
                        onclick={() => handleConnectTo(conn.id)}
                        disabled={connectionsStore.switching}
                      >
                        {connectionsStore.switching ? '...' : 'Connect'}
                      </Button>
                    {:else}
                      <span class="conn-active-label">Connected</span>
                    {/if}
                    <button class="conn-action-btn" onclick={() => handleEditConnection(conn)} title="Edit">
                      <Icon name="edit" size={14} />
                    </button>
                    <button class="conn-action-btn danger" onclick={() => handleDeleteConnection(conn.id)} title="Delete">
                      <Icon name="trash" size={14} />
                    </button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}

        {#if showSaveInput}
          <div class="save-input-row">
            <input
              class="save-name-input"
              type="text"
              bind:value={saveConnectionName}
              placeholder="Connection name (e.g. Work, Personal)"
              onkeydown={(e) => e.key === 'Enter' && handleSaveCurrentConnection()}
            />
            <Button variant="primary" size="sm" onclick={handleSaveCurrentConnection} disabled={!saveConnectionName.trim()}>
              Save
            </Button>
            <Button variant="secondary" size="sm" onclick={() => { showSaveInput = false; saveConnectionName = ''; }}>
              Cancel
            </Button>
          </div>
        {:else}
          <Button variant="secondary" size="sm" onclick={() => (showSaveInput = true)}>
            <Icon name="plus" size={14} />
            Save Current Connection
          </Button>
        {/if}
      </div>

      <div class="section-divider"></div>

      {#if backendProcessStore.isManagedBackend}
        <div class="auto-config-notice">
          <span class="status-dot connected"></span>
          <span>Local source-checkout backend is managed by the desktop shell.</span>
        </div>
        <button
          class="advanced-toggle"
          onclick={() => {
            connectionAdvancedTouched = true;
            showConnectionAdvanced = !showConnectionAdvanced;
          }}
        >
          {showConnectionAdvanced ? 'Hide' : 'Show'} Advanced Connection Settings
        </button>
      {/if}

      {#if showConnectionAdvanced}
        <div class="field">
          <label for="api-url">API URL</label>
          <input
            id="api-url"
            type="text"
            bind:value={apiUrl}
            placeholder="http://localhost:8000"
          />
          <p class="hint">The URL of your Nymeria API server</p>
        </div>

        <div class="field">
          <label for="api-key">Account Token</label>
          <input
            id="api-key"
            type="password"
            bind:value={apiKey}
            placeholder="nym_..."
          />
          <p class="hint">Per-user account token (<code>nym_...</code>) minted via <code>python run.py users add</code>, or the bootstrap admin token from <code>BOOTSTRAP_TOKEN.txt</code></p>
        </div>

        <div class="actions">
          <Button variant="secondary" onclick={handleTestConnection} disabled={testStatus === 'testing'}>
            {testStatus === 'testing' ? 'Testing...' : 'Test Connection'}
          </Button>
          {#if editingConnectionId}
            <Button variant="primary" onclick={handleUpdateConnection}>
              Update Connection
            </Button>
            <Button variant="secondary" onclick={handleCancelEdit}>
              Cancel Edit
            </Button>
          {:else}
            <Button variant="primary" onclick={handleSaveConnection}>
              Save
            </Button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Proxy Tab (CLIProxy management, source-checkout Tauri only) -->
  {#if activeTab === 'proxy' && isAdmin && backendProcessStore.isManagedBackend}
    <CLIProxyPanel />
  {/if}

  <!-- Account Tab (current user identity + sign out) -->
  {#if activeTab === 'account'}
    <div class="tab-content">
      <AccountTab />
    </div>
  {/if}

  <!-- Users Tab (admin only) -->
  {#if activeTab === 'users' && isAdmin}
    <div class="tab-content">
      <UsersTab />
    </div>
  {/if}

  <!-- Appearance Tab -->
  {#if activeTab === 'appearance'}
    <div class="tab-content">
      <div class="field">
        <span class="field-label">Theme</span>
        <p class="hint">Choose a color scheme for the interface</p>
        <div class="theme-grid">
          {#each themeList as theme}
            {@const colors = getThemePreviewColors(theme.id)}
            <button
              class="theme-card"
              class:selected={selectedTheme === theme.id}
              onclick={() => handleThemeChange(theme.id)}
            >
              <div class="theme-preview" style="background: {colors.bg};">
                <div class="preview-accent" style="background: {colors.accent};"></div>
                <div class="preview-text" style="color: {colors.text};">Aa</div>
              </div>
              <div class="theme-info">
                <span class="theme-name">{theme.name}</span>
                <span class="theme-desc">{theme.description}</span>
              </div>
            </button>
          {/each}
        </div>
      </div>

      <div class="field">
        <span class="field-label">Message bubbles</span>
        <p class="hint">Show a soft background behind AI responses, or let them flow flat on the page.</p>
        <label class="bubble-toggle">
          <input
            type="checkbox"
            checked={showChatBubbles}
            onchange={(e) => handleChatBubblesChange((e.currentTarget as HTMLInputElement).checked)}
          />
          <span>Show message bubble around AI responses</span>
        </label>
      </div>

      <div class="field">
        <span class="field-label">Font (testing)</span>
        <p class="hint">Try different body fonts. The Nymeria logo is unaffected.</p>
        <div class="font-grid">
          {#each fontOptions as opt}
            <button
              class="font-card"
              class:selected={selectedFontId === opt.id}
              onclick={() => handleFontChange(opt.id)}
            >
              <span class="font-sample" style="font-family: {opt.stack};">Aa Bb Cc</span>
              <span class="font-name" style="font-family: {opt.stack};">{opt.name}</span>
              <span class="font-desc">{opt.description}</span>
            </button>
          {/each}
        </div>
      </div>

      <div class="field">
        <span class="field-label">Logo font (testing)</span>
        <p class="hint">Try alternative fonts for the Nymeria&#8202;OS wordmark in the sidebar. Temporary picker — pick a favourite and we'll bake it in.</p>
        <div class="font-grid">
          {#each logoFontOptions as opt}
            <button
              class="font-card logo-font-card"
              class:selected={selectedLogoFontId === opt.id}
              onclick={() => handleLogoFontChange(opt.id)}
            >
              <span class="logo-sample" style="font-family: {opt.stack}; font-size: {logoSize}px;">
                <span class="logo-sample-bold" style="font-weight: {logoWeightName}; opacity: {logoOpacityName / 100};">Nymeria</span><span class="logo-sample-light" style="font-weight: {logoWeightOs}; opacity: {logoOpacityOs / 100};">OS</span>
              </span>
              <span class="font-name" style="font-family: {opt.stack};">{opt.name}</span>
              <span class="font-desc">{opt.description}</span>
            </button>
          {/each}
        </div>
      </div>

      <div class="field">
        <div class="logo-tune-header">
          <span class="field-label">Logo tuning (testing)</span>
          <button class="logo-reset-all" onclick={resetAllLogoTuning} type="button">Reset all</button>
        </div>
        <p class="hint">Fine-tune the wordmark's size and the weight / opacity of &ldquo;Nymeria&rdquo; and &ldquo;OS&rdquo; independently. Changes apply live to the sidebar and to the previews above.</p>

        <div class="logo-stepper-list">
          <!-- Color -->
          <div class="logo-stepper-row">
            <span class="logo-stepper-label">Color</span>
            <div class="logo-color-controls" role="group" aria-label="Logo color">
              <button class="color-seg" class:active={logoColor === 'accent'} type="button" onclick={() => setLogoColor('accent')}>Accent</button>
              <button class="color-seg" class:active={logoColor === 'white'} type="button" onclick={() => setLogoColor('white')}>White</button>
              <button class="color-seg" class:active={logoColor === 'black'} type="button" onclick={() => setLogoColor('black')}>Black</button>
            </div>
            <button class="logo-reset-btn" type="button" disabled={logoColor === LOGO_COLOR_DEFAULT} onclick={resetLogoColor}>Reset</button>
          </div>

          <!-- Size -->
          <div class="logo-stepper-row">
            <span class="logo-stepper-label">Size</span>
            <div class="logo-stepper-controls">
              <button class="stepper-btn" type="button" aria-label="Decrease size" disabled={logoSize <= LOGO_SIZE_MIN} onclick={() => setLogoSize(logoSize - 1)}>−</button>
              <span class="stepper-value">{logoSize}px</span>
              <button class="stepper-btn" type="button" aria-label="Increase size" disabled={logoSize >= LOGO_SIZE_MAX} onclick={() => setLogoSize(logoSize + 1)}>+</button>
            </div>
            <button class="logo-reset-btn" type="button" disabled={logoSize === LOGO_SIZE_DEFAULT} onclick={resetLogoSize}>Reset</button>
          </div>

          <!-- Nymeria weight -->
          <div class="logo-stepper-row">
            <span class="logo-stepper-label">&ldquo;Nymeria&rdquo; weight</span>
            <div class="logo-stepper-controls">
              <button class="stepper-btn" type="button" aria-label="Decrease Nymeria weight" disabled={logoWeightName <= LOGO_WEIGHT_STEPS[0]} onclick={() => stepLogoWeight('name', -1)}>−</button>
              <span class="stepper-value">{logoWeightName}</span>
              <button class="stepper-btn" type="button" aria-label="Increase Nymeria weight" disabled={logoWeightName >= LOGO_WEIGHT_STEPS[LOGO_WEIGHT_STEPS.length - 1]} onclick={() => stepLogoWeight('name', 1)}>+</button>
            </div>
            <button class="logo-reset-btn" type="button" disabled={logoWeightName === LOGO_WEIGHT_NAME_DEFAULT} onclick={resetLogoWeightName}>Reset</button>
          </div>

          <!-- Nymeria opacity -->
          <div class="logo-stepper-row">
            <span class="logo-stepper-label">&ldquo;Nymeria&rdquo; opacity</span>
            <div class="logo-stepper-controls">
              <button class="stepper-btn" type="button" aria-label="Decrease Nymeria opacity" disabled={logoOpacityName <= LOGO_OPACITY_MIN} onclick={() => setLogoOpacity('name', logoOpacityName - 10)}>−</button>
              <span class="stepper-value">{logoOpacityName}%</span>
              <button class="stepper-btn" type="button" aria-label="Increase Nymeria opacity" disabled={logoOpacityName >= LOGO_OPACITY_MAX} onclick={() => setLogoOpacity('name', logoOpacityName + 10)}>+</button>
            </div>
            <button class="logo-reset-btn" type="button" disabled={logoOpacityName === LOGO_OPACITY_DEFAULT} onclick={resetLogoOpacityName}>Reset</button>
          </div>

          <!-- OS weight -->
          <div class="logo-stepper-row">
            <span class="logo-stepper-label">&ldquo;OS&rdquo; weight</span>
            <div class="logo-stepper-controls">
              <button class="stepper-btn" type="button" aria-label="Decrease OS weight" disabled={logoWeightOs <= LOGO_WEIGHT_STEPS[0]} onclick={() => stepLogoWeight('os', -1)}>−</button>
              <span class="stepper-value">{logoWeightOs}</span>
              <button class="stepper-btn" type="button" aria-label="Increase OS weight" disabled={logoWeightOs >= LOGO_WEIGHT_STEPS[LOGO_WEIGHT_STEPS.length - 1]} onclick={() => stepLogoWeight('os', 1)}>+</button>
            </div>
            <button class="logo-reset-btn" type="button" disabled={logoWeightOs === LOGO_WEIGHT_OS_DEFAULT} onclick={resetLogoWeightOs}>Reset</button>
          </div>

          <!-- OS opacity -->
          <div class="logo-stepper-row">
            <span class="logo-stepper-label">&ldquo;OS&rdquo; opacity</span>
            <div class="logo-stepper-controls">
              <button class="stepper-btn" type="button" aria-label="Decrease OS opacity" disabled={logoOpacityOs <= LOGO_OPACITY_MIN} onclick={() => setLogoOpacity('os', logoOpacityOs - 10)}>−</button>
              <span class="stepper-value">{logoOpacityOs}%</span>
              <button class="stepper-btn" type="button" aria-label="Increase OS opacity" disabled={logoOpacityOs >= LOGO_OPACITY_MAX} onclick={() => setLogoOpacity('os', logoOpacityOs + 10)}>+</button>
            </div>
            <button class="logo-reset-btn" type="button" disabled={logoOpacityOs === LOGO_OPACITY_DEFAULT} onclick={resetLogoOpacityOs}>Reset</button>
          </div>
        </div>
      </div>

      <div class="field checkbox-field">
        <input
          id="global-show-autonomous-prompts"
          type="checkbox"
          checked={configStore.showAutonomousPrompts}
          onchange={(e) => (configStore.showAutonomousPrompts = e.currentTarget.checked)}
        />
        <label for="global-show-autonomous-prompts">Show autonomous prompts</label>
        <p class="hint">
          Show the prompts sent by the scheduler, watchdog, and triggers as
          messages in chat (both live and in history). You can still force
          this on for a specific thread from its Thread Settings.
        </p>
      </div>

      <div class="field checkbox-field">
        <input
          id="global-describe-tool-calls"
          type="checkbox"
          checked={configStore.describeToolCalls}
          onchange={(e) => (configStore.describeToolCalls = e.currentTarget.checked)}
        />
        <label for="global-describe-tool-calls">Describe tool calls</label>
        <p class="hint">
          Show a short plain-English summary next to each tool name on the
          collapsed tool cards, so you can follow what the agent is doing
          without expanding them.
        </p>
      </div>
    </div>
  {/if}

  <!-- Provider Tab -->
  {#if activeTab === 'llm' && isAdmin}
    <div class="tab-content">
      {#if loadingSettings}
        <p class="loading">Loading settings...</p>
      {:else}
        <div class="provider-setup-callout">
          <div>
            <span class="section-title">Provider Setup</span>
            <p class="hint">Test and save a direct provider key or configure a backend to use an existing CLIProxy OAuth endpoint.</p>
          </div>
          <Button variant="secondary" onclick={() => (showProviderSetupWizard = true)}>
            <Icon name="settings" size={14} />
            Open Wizard
          </Button>
        </div>

        <div class="field">
          <label for="llm-provider">Provider</label>
          <ProviderSelect
            id="llm-provider"
            bind:value={displayProvider}
            groups={settingsProviderGroups}
          />
          <p class="hint">
            {#if displayProvider === 'anthropic_proxy'}
              Routes through CLIProxy using your Claude subscription
            {:else if displayProvider === 'anthropic_direct'}
              Direct Anthropic API. Pay-per-token (requires ANTHROPIC_API_KEY)
            {:else if displayProvider === 'local_openai'}
              Local OpenAI-compatible server (e.g. llama.cpp llama-server, LM Studio). Edit the API Base URL under Advanced Settings.
            {:else if displayProvider === 'openai_custom'}
              OpenAI-compatible endpoint such as CLIProxy Codex OAuth. Edit the API Base URL under Advanced Settings.
            {:else}
              LLM provider. Save credentials in Connections or set the provider key in the server environment.
            {/if}
          </p>
        </div>

        {#if showProviderRouteSelect()}
          <div class="field">
            <label for="llm-provider-route">Provider Route</label>
            <select id="llm-provider-route" bind:value={llmProviderRoute}>
              {#each supportedRoutesForProvider(llmProvider, providerCatalog) as route}
                <option value={route}>{providerRouteLabel(route)}</option>
              {/each}
            </select>
            <p class="hint">
              {#if llmProviderRoute === 'openai_compat'}
                Uses the provider's OpenAI-compatible API surface.
              {:else}
                Uses the dedicated LangChain provider package when available.
              {/if}
            </p>
          </div>
        {/if}

        <div class="field">
          <label for="llm-model">Model</label>
          {#if availableModelsState.models.length > 0}
            <select id="llm-model" bind:value={llmModel}>
              {#each availableModelsState.models as model}
                <option value={model.id}>{model.name || model.id}</option>
              {/each}
            </select>
            <p class="hint">{availableModelsState.models.length} models available</p>
          {:else if availableModelsState.loading}
            <select id="llm-model" disabled>
              <option>Loading models...</option>
            </select>
            <p class="hint">Fetching available models...</p>
          {:else}
            <select id="llm-model" bind:value={llmModel}>
              {#if (modelOptions[llmProvider] ?? []).length > 0}
                {#each modelOptions[llmProvider] ?? [] as model}
                  <option value={model.value}>{model.label}</option>
                {/each}
              {:else}
                <option value={llmModel}>{llmModel || 'Type a model ID below'}</option>
              {/if}
            </select>
          {/if}
          <div class="model-custom-row">
            <input
              type="text"
              bind:value={llmModel}
              placeholder={
                displayProvider === 'local_openai' ? 'Local model alias (e.g. local-llm)' :
                llmProvider === 'openrouter' ? 'e.g. meta-llama/llama-4-scout' :
                'Custom model ID'
              }
              class="model-custom"
            />
            <button
              type="button"
              class="info-btn"
              title="How to use a custom model"
              onclick={() => showModelHelp = !showModelHelp}
            >
              <Icon name="info" size={14} />
            </button>
          </div>
          <p class="hint">
            {#if displayProvider === 'local_openai'}
              Enter the model alias your local server reports (the <code>--alias</code> flag value, or the model file basename)
            {:else if llmProvider === 'openrouter'}
              Use the dropdown or paste a model ID from <a href="https://openrouter.ai/models" target="_blank" rel="noopener">openrouter.ai/models</a>
            {:else if llmProvider === 'openai'}
              Use the live model list when available, or enter an OpenAI model name
            {:else}
              Use the live model list when available, or enter an exact model ID
            {/if}
          </p>
          {#if showModelHelp}
            <div class="model-help-box">
              <strong>Using a custom model</strong>
              {#if llmProvider === 'openrouter'}
                <ol>
                  <li>Go to <a href="https://openrouter.ai/models" target="_blank" rel="noopener">openrouter.ai/models</a></li>
                  <li>Find the model you want and open its page</li>
                  <li>Copy the model ID (e.g. <code>meta-llama/llama-4-scout</code>)</li>
                  <li>Paste it into the text field above</li>
                </ol>
                <p>The model ID is shown at the top of every model page on OpenRouter, in the format <code>provider/model-name</code>.</p>
              {:else}
                <ol>
                  <li>Find the model name in your provider's documentation</li>
                  <li>Enter the exact model identifier in the text field above</li>
                </ol>
              {/if}
            </div>
          {/if}
          {#if currentModelMeta}
            <div class="model-meta-hint">
              <span class="meta-name">{currentModelMeta.name}</span>
              <span class="meta-details">
                {modelsStore.formatContext(currentModelMeta.context_length)} ctx
                {#if currentModelMeta.pricing_prompt != null}
                  &middot; In: {modelsStore.formatPrice(currentModelMeta.pricing_prompt)}
                {/if}
                {#if currentModelMeta.pricing_completion != null}
                  &middot; Out: {modelsStore.formatPrice(currentModelMeta.pricing_completion)}
                {/if}
                {#if currentModelMeta.input_modalities.includes('image')}
                  &middot; Vision
                {/if}
                {#if currentModelMeta.supported_parameters.includes('reasoning')}
                  &middot; Reasoning
                {/if}
              </span>
            </div>
          {/if}
        </div>

        <div class="field">
          <label class="toggle-label" for="llm-use-model-defaults">
            <input type="checkbox" id="llm-use-model-defaults" bind:checked={llmUseModelDefaults} />
            Use model defaults
          </label>
          <p class="hint">
            Let the provider apply model-specific optimal defaults for temperature, top_p, and frequency penalty
            {#if llmUseModelDefaults && currentModelMeta}
              {#if currentModelMeta.default_temperature != null}
                &mdash; temp: {currentModelMeta.default_temperature}
              {/if}
              {#if currentModelMeta.default_top_p != null}
                , top_p: {currentModelMeta.default_top_p}
              {/if}
            {/if}
          </p>
        </div>

        <div class="field" class:field-disabled={llmUseModelDefaults}>
          <label for="llm-temperature">Temperature: {llmTemperature}</label>
          <input
            id="llm-temperature"
            type="range"
            min="0"
            max="2"
            step="0.1"
            bind:value={llmTemperature}
            disabled={llmUseModelDefaults}
          />
          <p class="hint">0 = deterministic, 2 = creative</p>
        </div>

        <div class="field">
          <label class="toggle-label" for="llm-extended-thinking">
            <input type="checkbox" id="llm-extended-thinking" bind:checked={llmExtendedThinking} />
            Enable Reasoning
          </label>
          <p class="hint">Enable/disable thinking tokens for compatible models</p>
        </div>

        {#if showOpenAiApiMode()}
          <div class="field">
            <label for="openai-api-mode">API Mode</label>
            <select id="openai-api-mode" bind:value={openaiApiMode}>
              <option value="responses">Responses API</option>
              <option value="chat_completions">Chat Completions (not recommended if thinking is enabled)</option>
            </select>
            <p class="hint">Responses API is the default path for OpenAI reasoning models, CLIProxy Codex OAuth, and OpenRouter beta.</p>
          </div>
        {/if}

        <!-- Advanced Settings Collapsible -->
        <div class="advanced-section">
          <button
            class="advanced-toggle"
            onclick={() => (showAdvancedLlm = !showAdvancedLlm)}
          >
            <span class="toggle-icon">{showAdvancedLlm ? '▼' : '▶'}</span>
            Advanced Settings
          </button>

          {#if showAdvancedLlm}
            <div class="advanced-content">
              <div class="field">
                <label for="llm-max-tokens">Max Tokens (optional)</label>
                <input
                  id="llm-max-tokens"
                  type="number"
                  min="1"
                  max="32000"
                  placeholder="Default (model limit)"
                  bind:value={llmMaxTokens}
                />
                <p class="hint">Maximum output tokens (1-32000)</p>
              </div>

              <div class="field" class:field-disabled={llmUseModelDefaults}>
                <label for="llm-top-p">Top P: {llmTopP !== null ? llmTopP.toFixed(2) : 'Default'}</label>
                <div class="slider-with-clear">
                  <input
                    id="llm-top-p"
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={llmTopP ?? 1}
                    oninput={(e) => (llmTopP = parseFloat(e.currentTarget.value))}
                    disabled={llmUseModelDefaults}
                  />
                  <button class="clear-btn" onclick={() => (llmTopP = null)} title="Reset to default" disabled={llmUseModelDefaults}>×</button>
                </div>
                <p class="hint">
                  Nucleus sampling threshold (0-1)
                  {#if llmUseModelDefaults && currentModelMeta?.default_top_p != null}
                    &mdash; Model default: {currentModelMeta.default_top_p}
                  {/if}
                </p>
              </div>

              <div class="field">
                <label for="llm-top-k">Top K (optional)</label>
                <input
                  id="llm-top-k"
                  type="number"
                  min="1"
                  max="100"
                  placeholder="Default"
                  bind:value={llmTopK}
                />
                <p class="hint">Top-k sampling (1-100)</p>
              </div>

              <div class="field" class:field-disabled={llmUseModelDefaults}>
                <label for="llm-freq-penalty">Frequency Penalty: {llmFrequencyPenalty !== null ? llmFrequencyPenalty.toFixed(1) : 'Default'}</label>
                <div class="slider-with-clear">
                  <input
                    id="llm-freq-penalty"
                    type="range"
                    min="-2"
                    max="2"
                    step="0.1"
                    value={llmFrequencyPenalty ?? 0}
                    oninput={(e) => (llmFrequencyPenalty = parseFloat(e.currentTarget.value))}
                    disabled={llmUseModelDefaults}
                  />
                  <button class="clear-btn" onclick={() => (llmFrequencyPenalty = null)} title="Reset to default" disabled={llmUseModelDefaults}>×</button>
                </div>
                <p class="hint">
                  Reduce repetition of token sequences (-2 to 2)
                  {#if llmUseModelDefaults && currentModelMeta?.default_frequency_penalty != null}
                    &mdash; Model default: {currentModelMeta.default_frequency_penalty}
                  {/if}
                </p>
              </div>

              <div class="field" class:field-disabled={llmUseModelDefaults}>
                <label for="llm-pres-penalty">Presence Penalty: {llmPresencePenalty !== null ? llmPresencePenalty.toFixed(1) : 'Default'}</label>
                <div class="slider-with-clear">
                  <input
                    id="llm-pres-penalty"
                    type="range"
                    min="-2"
                    max="2"
                    step="0.1"
                    value={llmPresencePenalty ?? 0}
                    oninput={(e) => (llmPresencePenalty = parseFloat(e.currentTarget.value))}
                    disabled={llmUseModelDefaults}
                  />
                  <button class="clear-btn" onclick={() => (llmPresencePenalty = null)} title="Reset to default" disabled={llmUseModelDefaults}>×</button>
                </div>
                <p class="hint">Encourage new topics (-2 to 2)</p>
              </div>

              <div class="field">
                <label for="llm-reasoning">Reasoning Effort</label>
                <select id="llm-reasoning" bind:value={llmReasoningEffort}>
                  <option value={null}>Default</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
                <p class="hint">For reasoning models (o1, Claude with thinking)</p>
              </div>

              <div class="field">
                <label for="llm-base-url">API Base URL (optional)</label>
                <input
                  id="llm-base-url"
                  type="text"
                  placeholder={displayProvider === 'openai_custom' ? DEFAULT_OPENAI_CLIPROXY_BASE_URL : "Default (provider's standard URL)"}
                  bind:value={llmBaseUrl}
                />
                <p class="hint">
                  Override the API endpoint (e.g., <code>http://cli-proxy-api-latest:8317/v1</code> for OpenAI/Codex CLIProxy).
                  Leave empty to use the provider's default URL.
                </p>
              </div>

              <div class="field">
                <label for="llm-context-length">Context Window Tokens</label>
                <input
                  id="llm-context-length"
                  type="number"
                  min="1000"
                  max="2000000"
                  step="1"
                  bind:value={llmContextLength}
                  placeholder="Auto-detect"
                />
                <p class="hint">Manual context window override for local servers or proxies that do not report it.</p>
              </div>

              <div class="field">
                <label for="llm-ollama-num-ctx">Ollama num_ctx</label>
                <input
                  id="llm-ollama-num-ctx"
                  type="number"
                  min="1000"
                  max="2000000"
                  step="1"
                  bind:value={llmOllamaNumCtx}
                  placeholder="Auto-detect"
                />
                <p class="hint">Passed to Ollama as options.num_ctx. Leave empty unless you need a VRAM cap.</p>
              </div>

              <div class="field">
                <label class="toggle-label" for="dynamic-tool-binding">
                  <input type="checkbox" id="dynamic-tool-binding" bind:checked={dynamicToolBinding} />
                  Dynamic tool binding
                </label>
                <p class="hint">
                  Default. Resolves tools per-step in the model node instead of
                  rebuilding the graph when tools are enabled mid-turn. Newly
                  created or installed tools are resolved from the live registry
                  before dispatch. Uncheck to force the legacy
                  <code>tool_reload_resume</code> rebuild path for the whole
                  installation.
                </p>
              </div>

            </div>
          {/if}
        </div>

        <div class="actions">
          <Button variant="primary" onclick={handleSaveServerSettings} disabled={savingSettings}>
            {savingSettings ? 'Saving...' : 'Save LLM Settings'}
          </Button>
        </div>
      {/if}
    </div>
  {/if}

  <!-- Agent Tab -->
  {#if activeTab === 'agent' && isAdmin}
    <div class="tab-content">
      {#if loadingSettings}
        <p class="loading">Loading settings...</p>
      {:else}
        <div class="field">
          <label for="context-management">Context Management</label>
          <select id="context-management" bind:value={contextManagement}>
            <option value="auto_compact">Auto-Compact (default)</option>
            <option value="sliding_window">Sliding Window</option>
            <option value="none">None</option>
          </select>
          <p class="hint">
            {#if contextManagement === 'auto_compact'}
              Automatically summarizes old messages when context gets large
            {:else if contextManagement === 'sliding_window'}
              Keeps only the N most recent conversation cycles
            {:else}
              No context management. Conversation history grows unbounded
            {/if}
          </p>
        </div>

        {#if contextManagement === 'auto_compact'}
          <div class="field">
            <label for="compact-threshold-mode">Auto-Compact Trigger</label>
            <select id="compact-threshold-mode" bind:value={compactThresholdMode}>
              <option value="percentage">Percentage of context window</option>
              <option value="tokens">Absolute input-token count</option>
            </select>
            <p class="hint">
              {#if compactThresholdMode === 'percentage'}
                Triggers at a fraction of the model's context window.
              {:else}
                Triggers at a fixed input-token count regardless of model. Clamped to the model's context window.
              {/if}
            </p>
          </div>

          {#if compactThresholdMode === 'percentage'}
            <div class="field">
              <label for="compact-threshold">Auto-Compact Threshold: {Math.round(compactThreshold * 100)}%</label>
              <input
                id="compact-threshold"
                type="range"
                min="0.05"
                max="0.95"
                step="0.01"
                bind:value={compactThreshold}
              />
              <p class="hint">Context usage percentage that triggers summarization</p>
            </div>
          {:else}
            <div class="field">
              <label for="compact-threshold-tokens">Auto-Compact Token Threshold: {compactThresholdTokens.toLocaleString()} tokens</label>
              <input
                id="compact-threshold-tokens"
                type="number"
                min="1000"
                max="2000000"
                step="1000"
                bind:value={compactThresholdTokens}
              />
              <p class="hint">Token count from the most recent provider response that triggers summarization (1,000-2,000,000)</p>
            </div>
          {/if}
        {/if}

        {#if contextManagement === 'sliding_window'}
          <div class="field">
            <label for="context-cycles">Context Window Cycles: {slidingWindowCycles}</label>
            <input
              id="context-cycles"
              type="range"
              min="1"
              max="20"
              step="1"
              bind:value={slidingWindowCycles}
            />
            <p class="hint">Number of conversation cycles to keep in context</p>
          </div>
        {/if}

        <div class="field">
          <label for="memory-char-limit">Memory Character Limit</label>
          <input
            id="memory-char-limit"
            type="number"
            min="1"
            max="2000000"
            step="500"
            bind:value={memoryCharLimit}
          />
          <p class="hint">Maximum saved characters for global memories and thread notepads before the agent must consolidate.</p>
        </div>

        <div class="field">
          <label for="log-level">Log Level</label>
          <select id="log-level" bind:value={logLevel}>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
        </div>

        <div class="field checkbox-field">
          <input
            id="watchdog-enabled"
            type="checkbox"
            bind:checked={watchdogEnabled}
          />
          <label for="watchdog-enabled">Enable Watchdog</label>
          <p class="hint">Monitor TODO staleness</p>
        </div>

        {#if watchdogEnabled}
          <div class="field">
            <label for="watchdog-interval">Watchdog Interval: {watchdogIntervalMinutes} min</label>
            <input
              id="watchdog-interval"
              type="range"
              min="5"
              max="60"
              step="5"
              bind:value={watchdogIntervalMinutes}
            />
          </div>

          <div class="field">
            <label for="staleness-minutes">Staleness Threshold: {todoStalenessMinutes} minutes</label>
            <input
              id="staleness-minutes"
              type="range"
              min="5"
              max="240"
              step="5"
              bind:value={todoStalenessMinutes}
            />
          </div>
        {/if}

        <div class="actions">
          <Button variant="primary" onclick={handleSaveServerSettings} disabled={savingSettings}>
            {savingSettings ? 'Saving...' : 'Save Agent Settings'}
          </Button>
        </div>
      {/if}
    </div>
  {/if}

  <!-- System Prompt Tab -->
  {#if activeTab === 'persona' && isAdmin}
    <div class="tab-content tab-tools-flex">
      <SystemPromptEditor />
    </div>
  {/if}

  <!-- Global Memory Tab -->
  {#if activeTab === 'memory'}
    <div class="tab-content tab-tools-flex">
      <GlobalMemoryEditor />
    </div>
  {/if}

  <!-- Tools Tab -->
  {#if activeTab === 'tools'}
    <div class="tab-content tab-tools-flex">
      <ToolManagementPanel />
    </div>
  {/if}

  <!-- MCP Tab -->
  {#if activeTab === 'mcp'}
    <div class="tab-content tab-tools-flex">
      <MCPManagementPanel />
    </div>
  {/if}

  <!-- Connections Tab -->
  {#if activeTab === 'credentials'}
    <div class="tab-content tab-tools-flex">
      <CredentialManagerPanel />
    </div>
  {/if}

  <!-- Skills Tab -->
  {#if activeTab === 'skills'}
    <div class="tab-content tab-content-full">
      <SkillsPanel />
    </div>
  {/if}

  {#if activeTab === 'notifications'}
    <div class="tab-content tab-content-full">
      <NotificationsPanel />
    </div>
  {/if}

  <!-- Voice Tab -->
  {#if activeTab === 'voice' && isAdmin}
    <div class="tab-content">
      {#if loadingSettings}
        <p class="loading">Loading settings...</p>
      {:else}
        <h3 class="section-heading">Text-to-Speech (TTS)</h3>

        <div class="field">
          <label for="tts-provider">TTS Provider</label>
          <select id="tts-provider" bind:value={ttsProvider}>
            <option value="none">None (disabled)</option>
            <option value="cartesia">Cartesia Sonic</option>
            <option value="gemini">Gemini TTS</option>
            <option value="openai">OpenAI</option>
            <option value="qwen3">Qwen3-TTS (Local)</option>
          </select>
          <p class="hint">
            {#if ttsProvider === 'cartesia'}
              Cartesia Sonic-3 with ultra-low latency (~90ms) and speed/emotion controls
            {:else if ttsProvider === 'gemini'}
              Google Gemini 3.1 Flash TTS with 30 voices, 70+ languages, and audio tag support
            {:else if ttsProvider === 'openai'}
              Uses OpenAI TTS API (tts-1, tts-1-hd)
            {:else if ttsProvider === 'qwen3'}
              Local Qwen3-TTS via OpenAI-compatible server
            {:else}
              TTS disabled. Voice endpoints will not return audio
            {/if}
          </p>
        </div>

        {#if ttsProvider !== 'none'}
          {#if ttsProvider !== 'gemini' && ttsProvider !== 'cartesia'}
            <div class="field">
              <label for="tts-base-url">Base URL</label>
              <input
                id="tts-base-url"
                type="text"
                bind:value={ttsBaseUrl}
                placeholder={ttsProvider === 'openai' ? 'https://api.openai.com/v1' : 'http://localhost:8880/v1'}
              />
              <p class="hint">Leave empty for default ({ttsProvider === 'openai' ? 'api.openai.com' : 'localhost:8880'})</p>
            </div>
          {/if}

          <div class="field">
            <label for="tts-model">Model</label>
            <input
              id="tts-model"
              type="text"
              bind:value={ttsModel}
              placeholder={ttsProvider === 'cartesia' ? 'sonic-3' : ttsProvider === 'gemini' ? 'gemini-3.1-flash-tts-preview' : ttsProvider === 'openai' ? 'tts-1-hd' : 'Qwen3-TTS-0.6B'}
            />
          </div>

          <div class="field">
            <label for="tts-voice">Voice</label>
            <input
              id="tts-voice"
              type="text"
              bind:value={ttsVoice}
              placeholder={ttsProvider === 'cartesia' ? 'Voice ID from play.cartesia.ai' : ttsProvider === 'gemini' ? 'Kore' : ttsProvider === 'openai' ? 'nova' : 'default'}
            />
            <p class="hint">
              {#if ttsProvider === 'cartesia'}
                Voice UUID from <a href="https://play.cartesia.ai/voices" target="_blank">play.cartesia.ai/voices</a>
              {:else if ttsProvider === 'gemini'}
                Options: Kore, Puck, Charon, Algenib, Leda, Orus, Zephyr, and more
              {:else if ttsProvider === 'openai'}
                Options: alloy, echo, fable, onyx, nova, shimmer
              {:else}
                Voice ID or reference audio path for Qwen3-TTS
              {/if}
            </p>
          </div>

          {#if ttsProvider !== 'gemini'}
            {#if ttsProvider !== 'cartesia'}
              <div class="field">
                <label for="tts-format">Output Format</label>
                <select id="tts-format" bind:value={ttsOutputFormat}>
                  <option value="mp3">MP3</option>
                  <option value="wav">WAV</option>
                  <option value="opus">Opus</option>
                  <option value="aac">AAC</option>
                </select>
              </div>
            {/if}

            <div class="field">
              <label for="tts-speed">Speed: {ttsSpeed.toFixed(2)}x</label>
              <input
                id="tts-speed"
                type="range"
                min="0.25"
                max="4.0"
                step="0.25"
                bind:value={ttsSpeed}
              />
            </div>
          {/if}
        {/if}

        <h3 class="section-heading">Speech-to-Text (STT)</h3>

        <div class="field">
          <label for="stt-provider">STT Provider</label>
          <select id="stt-provider" bind:value={sttProvider}>
            <option value="none">None (disabled)</option>
            <option value="openai">OpenAI</option>
            <option value="faster-whisper">Faster-Whisper (Local)</option>
          </select>
          <p class="hint">
            {#if sttProvider === 'openai'}
              Uses OpenAI transcription API (gpt-4o-mini-transcribe)
            {:else if sttProvider === 'faster-whisper'}
              Local faster-whisper via OpenAI-compatible server
            {:else}
              STT disabled. Voice endpoints will not accept audio
            {/if}
          </p>
        </div>

        {#if sttProvider !== 'none'}
          <div class="field">
            <label for="stt-base-url">Base URL</label>
            <input
              id="stt-base-url"
              type="text"
              bind:value={sttBaseUrl}
              placeholder={sttProvider === 'openai' ? 'https://api.openai.com/v1' : 'http://localhost:8003/v1'}
            />
            <p class="hint">Leave empty for default ({sttProvider === 'openai' ? 'api.openai.com' : 'localhost:8003'})</p>
          </div>

          <div class="field">
            <label for="stt-model">Model</label>
            <input
              id="stt-model"
              type="text"
              bind:value={sttModel}
              placeholder={sttProvider === 'openai' ? 'gpt-4o-mini-transcribe' : 'large-v3-turbo'}
            />
          </div>

          <div class="field">
            <label for="stt-language">Language Hint</label>
            <input
              id="stt-language"
              type="text"
              bind:value={sttLanguage}
              placeholder="en"
              style="max-width: 120px;"
            />
            <p class="hint">Optional ISO 639-1 code (e.g., en, es, de). Improves accuracy.</p>
          </div>
        {/if}

        <h3 class="section-heading">Watch / Default Thread</h3>

        <div class="field">
          <label for="voice-thread">Voice Thread</label>
          <select id="voice-thread" bind:value={voiceDefaultThreadId}>
            <option value="">watch-default (auto-created)</option>
            {#each threadsStore.threads as thread}
              <option value={thread.id}>{thread.title || thread.id}</option>
            {/each}
          </select>
          <p class="hint">Thread used by the watch app and /voice/chat endpoint when no thread is specified</p>
        </div>

        <Button onclick={handleSaveServerSettings} disabled={savingSettings}>
          {savingSettings ? 'Saving...' : 'Save Voice Settings'}
        </Button>
      {/if}
    </div>
  {/if}

  <!-- Status message -->
  {#if testMessage}
    <div class="message" class:success={testStatus === 'success'} class:error={testStatus === 'error'}>
      {#if testStatus === 'success'}
        <Icon name="success" size={16} />
      {:else if testStatus === 'error'}
        <Icon name="error" size={16} />
      {/if}
      {testMessage}
    </div>
  {/if}

      </div>
      {/key}
    </main>
  </div>

  <ProviderSetupWizard
    isOpen={showProviderSetupWizard}
    currentSettings={serverSettings}
    onClose={() => (showProviderSetupWizard = false)}
    onSaved={handleProviderSetupSaved}
  />
</div>

<style>
  .settings-panel {
    display: flex;
    flex-direction: column;
    /* Fixed dimensions so tabs never resize the modal. 1080×720 gives the
       content column more breathing room while staying inside Modal's
       90vw/90vh ceiling on typical desktop windows. */
    width: 1080px;
    height: 720px;
    max-width: 90vw;
    max-height: 90vh;
    /* Pull flush against the parent Modal's content padding so the sidebar
       can run edge-to-edge with the modal frame. */
    margin: calc(-1 * var(--spacing-lg));
  }

  .settings-layout {
    display: flex;
    flex: 1;
    min-height: 0;
    gap: 0;
  }

  /* --- Left sidebar --- */
  .settings-sidebar {
    flex: 0 0 220px;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    padding: var(--spacing-md) var(--spacing-sm) var(--spacing-lg) var(--spacing-md);
    border-right: 1px solid var(--border-subtle);
    background: var(--bg-base);
    overflow-y: auto;
  }

  .nav-group {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .nav-group-label {
    font-size: var(--font-size-3xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    padding: 0 var(--spacing-sm) 4px;
  }

  .nav-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 7px 10px;
    background: transparent;
    border: none;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    cursor: pointer;
    text-align: left;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .nav-item :global(svg) {
    flex-shrink: 0;
    color: var(--text-muted);
    transition: color var(--transition-fast);
  }

  .nav-item:hover:not(:disabled) {
    background: var(--bg-hover);
    color: var(--text-primary);
  }
  .nav-item:hover:not(:disabled) :global(svg) {
    color: var(--text-secondary);
  }

  .nav-item.active {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }
  .nav-item.active :global(svg) {
    color: var(--accent-primary);
  }

  .nav-item:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* --- Right content area --- */
  .settings-content {
    flex: 1;
    min-width: 0;
    overflow-y: auto;
    padding: var(--spacing-lg) var(--spacing-lg) var(--spacing-lg) var(--spacing-lg);
  }

  /* Wrapper around all tab content. `{#key activeTab}` re-mounts this on
     every tab change, which fires the animation below — giving a subtle
     fade + slight upward slide so the swap feels smooth rather than
     snapping. Matches the right-panel tab swap pattern. */
  .tab-fade {
    animation: tabFade 160ms cubic-bezier(0.4, 0, 0.2, 1);
    height: 100%;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  @keyframes tabFade {
    from { opacity: 0; transform: translateY(3px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .tab-content {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    /* Fill the content area so each tab uses identical space and the panel
       can't resize when switching between tabs. */
    height: 100%;
    min-height: 0;
  }

  /* Previously these two modifiers imposed their own heights / min-widths
     which made the modal grow on certain tabs. Now they just fill the
     fixed content frame so layout stays identical across every tab. */
  .tab-content-full {
    min-height: 0;
  }

  .tab-tools-flex {
    min-height: 0;
    flex: 1;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .section-heading {
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
    margin: var(--spacing-sm) 0 0 0;
    padding-bottom: var(--spacing-xs);
    border-bottom: 1px solid var(--border-subtle);
  }

  .checkbox-field {
    flex-direction: row;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .checkbox-field label {
    margin: 0;
  }

  .checkbox-field .hint {
    margin-left: auto;
  }

  label,
  .field-label {
    font-weight: 500;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  input[type='text'],
  input[type='password'],
  select {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
  }

  input[type='text']:focus,
  input[type='password']:focus,
  select:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.15);
  }

  /* Custom range slider — replaces the browser's chunky default with a thin
     rounded track + accent-coloured thumb. accent-color is kept for Firefox
     (it fills the progress portion natively); WebKit doesn't fill a styled
     track, so the prominent thumb is what indicates position there. */
  input[type='range'] {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 18px;
    background: transparent;
    cursor: pointer;
    accent-color: var(--accent-primary);
  }

  input[type='range']:focus {
    outline: none;
  }

  /* Track — WebKit */
  input[type='range']::-webkit-slider-runnable-track {
    height: 4px;
    background: var(--border-subtle);
    border-radius: 2px;
    border: none;
  }

  /* Track — Firefox */
  input[type='range']::-moz-range-track {
    height: 4px;
    background: var(--border-subtle);
    border-radius: 2px;
    border: none;
  }

  /* Filled portion left of the thumb — Firefox only (WebKit ignores this
     when the track is custom-styled). */
  input[type='range']::-moz-range-progress {
    height: 4px;
    background: var(--accent-primary);
    border-radius: 2px;
  }

  /* Thumb — WebKit */
  input[type='range']::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--accent-primary);
    border: 2px solid var(--bg-base);
    /* Pull the 14px thumb up so its centre lines up with the 4px track:
       (14 - 4) / 2 = 5px. */
    margin-top: -5px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25);
    cursor: pointer;
    transition: transform 120ms ease, box-shadow 120ms ease;
  }

  /* Thumb — Firefox */
  input[type='range']::-moz-range-thumb {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--accent-primary);
    border: 2px solid var(--bg-base);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25);
    cursor: pointer;
    transition: transform 120ms ease, box-shadow 120ms ease;
  }

  /* Hover / focus — thumb grows slightly and picks up an accent glow. */
  input[type='range']:hover::-webkit-slider-thumb,
  input[type='range']:focus::-webkit-slider-thumb {
    transform: scale(1.15);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-primary) 18%, transparent);
  }

  input[type='range']:hover::-moz-range-thumb,
  input[type='range']:focus::-moz-range-thumb {
    transform: scale(1.15);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-primary) 18%, transparent);
  }

  input[type='range']:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  input[type='checkbox'] {
    width: 18px;
    height: 18px;
    accent-color: var(--accent-primary);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    cursor: pointer;
  }

  select {
    cursor: pointer;
  }

  .model-custom-row {
    display: flex;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
    align-items: center;
  }

  .model-custom {
    flex: 1;
    font-size: var(--font-size-sm);
  }

  .info-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    flex-shrink: 0;
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-full);
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .info-btn:hover {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .model-help-box {
    margin-top: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.6;
  }

  .model-help-box strong {
    display: block;
    margin-bottom: var(--spacing-xs);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .model-help-box ol {
    margin: 0;
    padding-left: var(--spacing-lg);
  }

  .model-help-box li {
    margin-bottom: 2px;
  }

  .model-help-box p {
    margin: var(--spacing-xs) 0 0;
  }

  .model-help-box code {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    background: var(--bg-elevated);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
  }

  .model-help-box a,
  .hint a {
    color: var(--accent-secondary);
    text-decoration: none;
  }

  .model-help-box a:hover,
  .hint a:hover {
    text-decoration: underline;
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .model-meta-hint {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
    padding: 6px 10px;
    background: var(--glass-bg);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
  }

  .meta-name {
    color: var(--text-primary);
    font-weight: 500;
  }

  .meta-details {
    color: var(--text-muted);
  }

  .field-disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .loading {
    color: var(--text-secondary);
    font-style: italic;
  }

  .message {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .message.success {
    background: rgba(52, 211, 153, 0.15);
    color: var(--success);
  }

  .message.error {
    background: rgba(248, 113, 113, 0.15);
    color: var(--error);
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: flex-end;
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  /* Chat bubble toggle */
  .bubble-toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    cursor: pointer;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    user-select: none;
  }

  .bubble-toggle input {
    margin: 0;
    cursor: pointer;
    accent-color: var(--accent-primary);
  }

  /* Font picker styles (testing-only) */
  .font-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }

  .font-card {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 2px solid var(--border-subtle);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all 0.15s ease;
    text-align: left;
  }

  .font-card:hover {
    border-color: var(--border-default);
    background: var(--bg-hover);
  }

  .font-card.selected {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.15);
  }

  .font-sample {
    font-size: 22px;
    font-weight: 400;
    color: var(--text-primary);
    line-height: 1.1;
    letter-spacing: -0.01em;
  }

  .font-name {
    font-size: 13px;
    font-weight: 500;
    color: var(--text-primary);
  }

  .font-desc {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  /* Logo font card — wider so the wordmark fits comfortably */
  .logo-font-card {
    /* Inherits everything else from .font-card */
  }

  .logo-sample {
    font-size: var(--font-size-xl);
    color: var(--logo-color, var(--accent-primary));
    line-height: 1.1;
    letter-spacing: -0.02em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .logo-sample-bold {
    font-weight: 700;
  }

  .logo-sample-light {
    font-weight: 300;
  }

  /* Logo tuning steppers */
  .logo-tune-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
  }

  .logo-reset-all {
    padding: 4px 10px;
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .logo-reset-all:hover {
    border-color: var(--border-default);
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .logo-stepper-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: var(--spacing-sm);
  }

  .logo-stepper-row {
    display: grid;
    grid-template-columns: minmax(140px, 1fr) auto auto;
    align-items: center;
    gap: var(--spacing-md);
    padding: 8px 12px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .logo-stepper-label {
    font-size: 13px;
    color: var(--text-primary);
  }

  .logo-stepper-controls {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .stepper-btn {
    width: 26px;
    height: 26px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: var(--font-size-base);
    line-height: 1;
    cursor: pointer;
    transition: all 0.12s ease;
  }

  .stepper-btn:hover:not(:disabled) {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
  }

  .stepper-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .stepper-value {
    min-width: 48px;
    text-align: center;
    font-size: 13px;
    font-variant-numeric: tabular-nums;
    color: var(--text-primary);
  }

  .logo-reset-btn {
    padding: 4px 10px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .logo-reset-btn:hover:not(:disabled) {
    border-color: var(--border-subtle);
    color: var(--text-secondary);
    background: var(--bg-hover);
  }

  .logo-reset-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }

  /* Three-way segmented control for logo color */
  .logo-color-controls {
    display: inline-flex;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .color-seg {
    padding: 4px 12px;
    background: transparent;
    border: none;
    border-right: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    line-height: 1.4;
    cursor: pointer;
    transition: background 0.12s ease, color 0.12s ease;
  }

  .color-seg:last-child {
    border-right: none;
  }

  .color-seg:hover:not(.active) {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .color-seg.active {
    background: var(--accent-primary);
    color: var(--bg-base);
    font-weight: 500;
  }

  /* Theme selector styles */
  .theme-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }

  .theme-card {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 2px solid var(--border-subtle);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all 0.15s ease;
    text-align: left;
  }

  .theme-card:hover {
    border-color: var(--border-default);
    background: var(--bg-hover);
  }

  .theme-card.selected {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.15);
  }

  .theme-preview {
    position: relative;
    width: 100%;
    height: 48px;
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }

  .preview-accent {
    position: absolute;
    bottom: 0;
    left: 0;
    width: 100%;
    height: 4px;
  }

  .preview-text {
    font-size: var(--font-size-lg);
    font-weight: 600;
    opacity: 0.8;
  }

  .theme-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .theme-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .theme-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.3;
  }

  /* Advanced settings section */
  .advanced-section {
    margin-top: var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .advanced-toggle {
    width: 100%;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: none;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .advanced-toggle:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .toggle-icon {
    font-size: var(--font-size-3xs);
    color: var(--text-muted);
  }

  .advanced-content {
    padding: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  .slider-with-clear {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .slider-with-clear input[type='range'] {
    flex: 1;
  }

  .clear-btn {
    width: 24px;
    height: 24px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .clear-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--border-default);
  }

  input[type='number'] {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
  }

  input[type='number']:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.15);
  }

  input[type='number']::placeholder {
    color: var(--text-muted);
  }

  .auto-config-notice {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background: var(--bg-elevated-2);
    border-radius: 6px;
    border: 1px solid var(--border-default);
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin-bottom: 1rem;
  }

  .status-dot.connected {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--success, #22c55e);
    box-shadow: 0 0 6px var(--success, #22c55e);
    flex-shrink: 0;
  }

  .advanced-toggle {
    background: none;
    border: none;
    color: var(--accent-primary);
    font-size: 0.8125rem;
    cursor: pointer;
    padding: 0;
    margin-bottom: 1rem;
  }

  .advanced-toggle:hover {
    text-decoration: underline;
  }

  /* Saved Connections */
  .saved-connections {
    margin-bottom: var(--spacing-sm);
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--spacing-sm);
  }

  .section-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .section-divider {
    height: 1px;
    background: var(--glass-border);
    margin: var(--spacing-md) 0;
  }

  .provider-setup-callout {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
  }

  .provider-setup-callout > div {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .connections-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-sm);
  }

  .connection-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
    border: 1px solid transparent;
    transition: all var(--transition-fast);
  }

  .connection-row.active {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.05);
  }

  .connection-row:hover {
    background: var(--bg-hover);
  }

  .conn-status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-muted);
    flex-shrink: 0;
  }

  .conn-status-dot.connected {
    background: var(--success, #22c55e);
    box-shadow: 0 0 6px rgba(34, 197, 94, 0.4);
  }

  .conn-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .conn-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .conn-url {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .conn-name-input {
    flex: 1;
    min-width: 0;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    border: 1px solid var(--glass-border);
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .conn-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-shrink: 0;
  }

  .conn-action-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    transition: all var(--transition-fast);
  }

  .conn-action-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .conn-action-btn.danger:hover {
    color: var(--error, #ef4444);
  }

  .save-input-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-xs);
  }

  .save-name-input {
    flex: 1;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    border: 1px solid var(--glass-border);
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .conn-active-label {
    font-size: var(--font-size-xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-primary);
    white-space: nowrap;
  }
</style>
