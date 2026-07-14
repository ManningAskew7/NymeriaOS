<script lang="ts">
  import type { SkillMetadata, ThreadConfig, ThreadConfigUpdateRequest, UnifiedTool, LLMProviderSpec, ProviderRoute } from '$lib/types';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { hooksStore } from '$lib/stores/hooks.svelte';
  import {
    hookCategory,
    HOOK_ACTION_META,
    HOOK_CATEGORIES,
    HOOK_EVENT_META,
  } from '$lib/utils/hooks';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { loadAvailableModels, type AvailableModelsState } from '$lib/utils/models';
  import {
    REASONING_EFFORT_LEVELS,
    effortExceedsModelMax,
    effortOptionDisabled,
    reasoningEffortLabel,
    supportedEffortSet,
  } from '$lib/utils/reasoningEffort';
  import { buildMobileProviderGroups } from '$lib/utils/providerGroups';
  import { skillsStore } from '$lib/stores/skills.svelte';
  import { api } from '$lib/services/api.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import Icon from '$lib/components/common/Icon.svelte';
  import InlineLoader from '$lib/components/common/InlineLoader.svelte';
  import ProviderSelect from '$lib/components/common/ProviderSelect.svelte';
  import TriggerConfigTab from '$lib/components/triggers/TriggerConfigTab.svelte';
  import { ToolCountWarning } from '$lib/components/tools';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import { platformAfterCallableChange } from '$lib/utils/threadPlatform';
  import { filterToolSearch } from '$lib/utils/toolSearch';
  import MCPServerForm from '$lib/components/tools/MCPServerForm.svelte';
  import ConnectTelegramWizard from './ConnectTelegramWizard.svelte';
  import ConnectMyTelegramBotWizard from './ConnectMyTelegramBotWizard.svelte';
  import type { MCPServerCreateRequest } from '$lib/types';
  import {
    coerceProviderRoute,
    hasRouteChoice,
    providerRouteLabel,
    providerSpecFor,
    supportedRoutesForProvider,
  } from '$lib/utils/providerRoutes';
  import { untrack } from 'svelte';

  interface Props {
    threadId: string;
    open: boolean;
    onClose: () => void;
  }

  let { threadId, open, onClose }: Props = $props();

  type Tab = 'instructions' | 'system' | 'agent' | 'dream' | 'hooks' | 'model' | 'tools' | 'mcp' | 'skills' | 'triggers' | 'chatapp';
  type TelegramAutonomousDelivery = ThreadConfig['telegramAutonomousDelivery'];
  type InAppNotificationLevel = ThreadConfig['inAppNotificationLevel'];
  const DREAM_DEFAULT_MIN_INTERVAL_HOURS = 6;
  const DREAM_DEFAULT_MIN_IDLE_MINUTES = 30;
  const DREAM_DEFAULT_MIN_TURNS_SINCE_LAST = 10;
  // Tier-grouped picker options sourced from the live provider catalog. See
  // utils/providerGroups.ts.
  let activeTab = $state<Tab>('instructions');

  // Config loaded from API
  let threadConfig = $state<ThreadConfig | null>(null);
  let loading = $state(true);

  // Form state — Instructions
  let instructions = $state('');
  let injectTodosInPrompt = $state(false);
  let showAutonomousPrompts = $state(false);
  let showPromptMetadata = $state(false);
  let telegramAutonomousDelivery = $state<TelegramAutonomousDelivery>('full');
  let inAppNotificationLevel = $state<InAppNotificationLevel>('notify_only');
  let dreamEnabled = $state(false);
  let dreamMinIntervalHours = $state(String(DREAM_DEFAULT_MIN_INTERVAL_HOURS));
  let dreamMinIdleMinutes = $state(String(DREAM_DEFAULT_MIN_IDLE_MINUTES));
  let dreamMinTurnsSinceLast = $state(String(DREAM_DEFAULT_MIN_TURNS_SINCE_LAST));
  let dreamModel = $state('');
  let dreamRunning = $state(false);
  let dreamStatus = $state('');

  // Form state — Hooks (per-thread enablement). `hooksEnabled` null = inherit
  // the global setting; `hookOverrides` maps a hook id to a per-thread on/off.
  let hooksEnabled = $state<boolean | null>(null);
  let hookOverrides = $state<Record<string, boolean>>({});

  // Form state — System Prompt & Agent
  let systemPrompt = $state('');
  let isCallable = $state(false);
  let callableName = $state('');
  let callableDescription = $state('');
  let imageWindowSize = $state<string | number>('');

  // Form state — Model
  let llmProvider = $state('');
  let llmProviderRoute = $state<'default' | ProviderRoute>('default');
  let llmModel = $state('');
  let llmBaseUrl = $state('');
  let llmApiKey = $state('');
  let llmTemperature = $state('');
  let llmMaxTokens = $state('');
  let llmContextLength = $state('');
  let llmOllamaNumCtx = $state('');
  let llmExtendedThinking = $state<'default' | 'true' | 'false'>('default');
  let llmReasoningEffort = $state('');
  let llmUseModelDefaults = $state<'default' | 'true' | 'false'>('default');
  let llmOpenAiApiMode = $state<'default' | 'chat_completions' | 'responses'>('default');
  let compactThresholdMode = $state<'default' | 'percentage' | 'tokens'>('default');
  let compactThresholdPct = $state('');
  let compactThresholdTokens = $state('');
  let providerCatalog = $state<LLMProviderSpec[]>([]);
  let mobileThreadProviderGroups = $derived(buildMobileProviderGroups(providerCatalog));

  // Form state — Tools
  let disabledTools = $state<Set<string>>(new Set());
  let enabledTools = $state<Set<string>>(new Set());
  let toolSearch = $state('');
  let showToolWarning = $state(false);

  // Form state — Skills
  let threadEnabledSkills = $state<Set<string>>(new Set());
  let threadDisabledSkills = $state<Set<string>>(new Set());

  // Chat App state
  let showChatAppWizard = $state(false);
  let showMyBotWizard = $state(false);
  let chatAppLoaded = $state(false);
  let chatAppLoadError = $state('');

  let chatAppBindings = $derived(chatAppBindingsStore.getBindings(threadId));

  async function handleUnbindChatApp(bindingId: number) {
    try {
      await chatAppBindingsStore.unbind(threadId, bindingId);
      threadsStore.syncFromBackend();
    } catch (e) {
      chatAppLoadError = humanizeErrorText(e, { action: 'disconnect', resource: 'the chat app' });
    }
  }

  function isMcpToolName(name: string): boolean {
    return name.startsWith('mcp__');
  }

  // UI state
  let saving = $state(false);
  let error = $state('');

  // Model metadata (reactive)
  const threadModelMeta = $derived(modelsStore.getById(llmModel));

  // Effort-clamp warning targets the model this thread will actually use
  // (the override, else the inherited global default). Unsupported levels
  // are clamped server-side, so this is a hint, not an error.
  const effortModelMeta = $derived(
    modelsStore.getById(llmModel || serverSettingsStore.model || '')
  );
  const effortClampMax = $derived(effortModelMeta?.max_reasoning_effort ?? '');
  const effortSet = $derived(
    supportedEffortSet(effortModelMeta?.supported_reasoning_efforts)
  );
  const showEffortClampHint = $derived(
    effortExceedsModelMax(llmReasoningEffort, effortClampMax)
  );

  let availableModelsState = $state<AvailableModelsState>({
    models: [],
    provider: '',
    loading: false,
  });

  function getEffectiveProvider(): string {
    return llmProvider || serverSettingsStore.provider || '';
  }

  const fastTierRef = $derived(serverSettingsStore.fastModelResolved || '');
  const smartTierRef = $derived(serverSettingsStore.smartModelResolved || '');

  // Quick-pick: fill this thread's model (and provider, when the tier targets a
  // different one) from the resolved fast/smart tier. The backend only prefixes
  // provider: for cross-provider tiers, so a bare ref maps onto the model field.
  function applyTier(tier: 'fast' | 'smart') {
    const ref = tier === 'fast' ? fastTierRef : smartTierRef;
    if (!ref) return;
    const known = new Set([
      'anthropic', 'openai', 'openrouter', 'google', 'gemini', 'mistral',
      'groq', 'deepseek', 'xai', 'ollama', 'together', 'fireworks', 'custom',
      ...providerCatalog.map((p) => p.id),
    ]);
    const idx = ref.indexOf(':');
    if (idx > 0 && known.has(ref.slice(0, idx))) {
      llmProvider = ref.slice(0, idx);
      llmModel = ref.slice(idx + 1);
    } else {
      llmModel = ref;
    }
  }

  function selectedRoute(): ProviderRoute {
    const provider = getEffectiveProvider();
    const inherited = serverSettingsStore.providerRoute as ProviderRoute | null;
    const route = llmProviderRoute === 'default' ? inherited : llmProviderRoute;
    return coerceProviderRoute(provider, providerCatalog, route);
  }

  function showProviderRouteSelect(): boolean {
    return hasRouteChoice(getEffectiveProvider(), providerCatalog);
  }

  function supportsApiMode(provider: string = getEffectiveProvider()): boolean {
    if (!provider || provider === 'anthropic' || provider === 'bedrock') return false;
    if (hasRouteChoice(provider, providerCatalog)) return selectedRoute() === 'openai_compat';
    return provider !== 'google' && provider !== 'ollama';
  }

  function isClaudeModel(model: string): boolean {
    const id = (model || '').toLowerCase();
    return id.includes('claude') || id.startsWith('anthropic/');
  }

  // Gateways flagged anthropic_native_for_claude drop Claude's signed thinking on
  // their OpenAI-compatible path; nudge toward the Anthropic Messages route when a
  // Claude model is selected and that route is not already active.
  function showAnthropicRouteNudge(): boolean {
    const spec = providerSpecFor(providerCatalog, getEffectiveProvider());
    return (
      Boolean(spec?.anthropic_native_for_claude) &&
      isClaudeModel(llmModel) &&
      selectedRoute() !== 'anthropic_messages'
    );
  }

  // Base URL + API key apply on the OpenAI-compatible path and on a gateway's
  // Anthropic Messages route (both still target the gateway endpoint).
  function supportsConnectionOverride(): boolean {
    const provider = getEffectiveProvider();
    if (!provider) return false;
    return supportsApiMode(provider) || selectedRoute() === 'anthropic_messages';
  }

  $effect(() => {
    if (open) {
      void loadAvailableModels(getEffectiveProvider(), availableModelsState, llmBaseUrl);
    }
  });

  $effect(() => {
    if (open) {
      void api.getLLMProviderCatalog().then((catalog) => {
        providerCatalog = catalog;
      });
    }
  });

  // Effective tool count
  const effectiveToolCount = $derived.by(() => {
    const defaultNames = defaultToolsStore.defaultToolNames;
    const activeDefault = defaultNames.filter(n => !disabledTools.has(n)).length;
    return activeDefault + enabledTools.size;
  });

  // Tools not in the default set, excluding MCP tools
  const availableTools = $derived.by(() => {
    if (!defaultToolsStore.loaded) return [];
    const defaultSet = new Set(defaultToolsStore.defaultToolNames);
    return defaultToolsStore.tools
      .filter(t => !defaultSet.has(t.name) && !t.name.startsWith('mcp__'))
      .map(t => ({
        name: t.name,
        description: t.description,
        category: t.category,
        securityLevel: t.security_level,
        authStatus: t.auth_status ?? null,
        authProvider: t.auth_provider ?? null,
      }));
  });

  // MCP tools grouped by server. Default MCP tools can be disabled for this
  // thread; non-default MCP tools can be enabled for this thread.
  const mcpServersForThread = $derived.by(() => {
    if (!defaultToolsStore.loaded) return [] as {
      id: string;
      name: string;
      enabled: boolean;
      discoveredCount: number;
      tools: { name: string; shortName: string; description: string; isDefault: boolean }[];
    }[];
    const defaultSet = new Set(defaultToolsStore.defaultToolNames);
    return mcpServersStore.servers.map(server => ({
      id: server.id,
      name: server.name,
      enabled: server.enabled,
      discoveredCount: server.discoveredTools.length,
      tools: server.discoveredTools.map(tool => {
        const name = `mcp__${server.id}__${tool.name}`;
        return {
          name,
          shortName: tool.name,
          description: tool.description,
          isDefault: defaultSet.has(name),
        };
      }),
    }));
  });

  // MCP UI state
  let showMcpAddForm = $state(false);
  let mcpAddLoading = $state(false);
  let mcpAddError = $state<string | null>(null);
  let expandedMcpServer = $state<string | null>(null);

  function toggleAllMcpTools(tools: { name: string; isDefault?: boolean }[], enable: boolean) {
    const nextEnabled = new Set(enabledTools);
    const nextDisabled = new Set(disabledTools);
    for (const t of tools) {
      if (t.isDefault) {
        if (enable) nextDisabled.delete(t.name);
        else nextDisabled.add(t.name);
      } else {
        if (enable) nextEnabled.add(t.name);
        else nextEnabled.delete(t.name);
      }
    }
    enabledTools = nextEnabled;
    disabledTools = nextDisabled;
  }

  function areMcpToolsAllEnabled(tools: { name: string; isDefault?: boolean }[]): boolean {
    return tools.every(t => isMcpThreadToolEnabled(t));
  }

  function isMcpThreadToolEnabled(tool: { name: string; isDefault?: boolean }): boolean {
    return tool.isDefault ? !disabledTools.has(tool.name) : enabledTools.has(tool.name);
  }

  function toggleMcpThreadTool(tool: { name: string; isDefault?: boolean }) {
    if (tool.isDefault) {
      toggleTool(tool.name);
    } else {
      toggleAvailableTool(tool.name);
    }
  }

  async function handleMcpAdd(data: MCPServerCreateRequest) {
    mcpAddLoading = true;
    mcpAddError = null;
    try {
      const result = await mcpServersStore.create(data, threadId);
      if (result.discoveryError) {
        mcpAddError = `Server added but discovery failed: ${result.discoveryError}`;
      } else {
        showMcpAddForm = false;
        mcpAddError = null;
        defaultToolsStore.resetLoaded();
        await defaultToolsStore.load();
        const next = new Set(enabledTools);
        for (const tool of result.server.discoveredTools) {
          next.add(`mcp__${result.server.id}__${tool.name}`);
        }
        enabledTools = next;
      }
    } catch (e) {
      mcpAddError = humanizeErrorText(e, { action: 'create', resource: 'the server' });
    } finally {
      mcpAddLoading = false;
    }
  }

  // Filtered default tools
  const toolsLoadError = $derived(unifiedToolsStore.error || defaultToolsStore.error);
  const toolsReady = $derived(unifiedToolsStore.loaded && defaultToolsStore.loaded);
  const toolsLoading = $derived(unifiedToolsStore.loading || defaultToolsStore.loading || !toolsReady);

  const filteredTools = $derived.by(() => {
    if (!defaultToolsStore.loaded) return [];

    const defaultSet = new Set(defaultToolsStore.defaultToolNames);
    let allTools = unifiedToolsStore.tools.filter(t => defaultSet.has(t.name) && !isMcpToolName(t.name) && t.category !== 'mcp_server');
    return filterToolSearch(allTools, toolSearch, (tool: UnifiedTool) => ({
      id: tool.id,
      name: tool.name,
      description: tool.description,
      category: tool.category,
      tags: tool.tags,
      toolType: tool.toolType,
      implementationType: tool.implementationType,
    }));
  });

  const filteredAvailableTools = $derived.by(() =>
    filterToolSearch(availableTools, toolSearch, (tool) => ({
      name: tool.name,
      description: tool.description,
      category: tool.category,
      tags: [tool.securityLevel],
      toolType: 'builtin',
    }))
  );

  const disabledToolCount = $derived([...disabledTools].filter((name) => !isMcpToolName(name)).length);
  const enabledToolCount = $derived([...enabledTools].filter((name) => !isMcpToolName(name)).length);
  const mcpOverrideCount = $derived(
    [...disabledTools].filter(isMcpToolName).length +
    [...enabledTools].filter(isMcpToolName).length
  );

  const resolvedActiveSkillNames = $derived.by(() => {
    const seen = new Set<string>();
    for (const skill of skillsStore.installed) {
      if (skill.default_active && !threadDisabledSkills.has(skill.name)) {
        seen.add(skill.name);
      }
    }
    for (const name of skillsStore.enabledGlobal) {
      if (!threadDisabledSkills.has(name)) seen.add(name);
    }
    for (const name of threadEnabledSkills) {
      if (!threadDisabledSkills.has(name)) seen.add(name);
    }
    return seen;
  });

  function isSkillActiveHere(skill: SkillMetadata): boolean {
    return (
      skill.default_active ||
      skillsStore.enabledGlobal.includes(skill.name) ||
      threadEnabledSkills.has(skill.name)
    ) && !threadDisabledSkills.has(skill.name);
  }

  function toggleThreadSkillEnabled(name: string) {
    const next = new Set(threadEnabledSkills);
    if (next.has(name)) {
      next.delete(name);
    } else {
      next.add(name);
      if (threadDisabledSkills.has(name)) {
        const disabled = new Set(threadDisabledSkills);
        disabled.delete(name);
        threadDisabledSkills = disabled;
      }
    }
    threadEnabledSkills = next;
  }

  function toggleThreadSkillDisabled(name: string) {
    const next = new Set(threadDisabledSkills);
    if (next.has(name)) {
      next.delete(name);
    } else {
      next.add(name);
      if (threadEnabledSkills.has(name)) {
        const enabled = new Set(threadEnabledSkills);
        enabled.delete(name);
        threadEnabledSkills = enabled;
      }
    }
    threadDisabledSkills = next;
  }

  // Trigger count for badge
  const activeTriggerCount = $derived(
    triggersStore.triggers.filter(t => t.enabled && t.thread_id === threadId).length
  );

  // Hooks that apply to this thread (its own + globals) and the per-thread
  // override count, for the Hooks tab.
  const threadHooks = $derived(hooksStore.threadHooks(threadId));
  const hookGroups = $derived(
    HOOK_CATEGORIES.map((meta) => ({
      meta,
      hooks: threadHooks.filter((h) => hookCategory(h.action) === meta.key),
    })).filter((g) => g.hooks.length > 0)
  );
  const hookOverrideCount = $derived(Object.keys(hookOverrides).length);

  type HookMaster = 'inherit' | 'on' | 'off';
  const hooksMasterValue = $derived<HookMaster>(
    hooksEnabled === null || hooksEnabled === undefined ? 'inherit' : hooksEnabled ? 'on' : 'off'
  );
  function setHooksMaster(v: HookMaster) {
    hooksEnabled = v === 'inherit' ? null : v === 'on';
  }
  function hookOverrideValue(id: string): 'default' | 'on' | 'off' {
    const v = hookOverrides[id];
    return v === undefined ? 'default' : v ? 'on' : 'off';
  }
  function setHookOverride(id: string, v: 'default' | 'on' | 'off') {
    const next = { ...hookOverrides };
    if (v === 'default') delete next[id];
    else next[id] = v === 'on';
    hookOverrides = next;
  }

  // Load config and deps
  $effect(() => {
    if (open && threadId) {
      untrack(() => loadConfig());
    }
  });

  $effect(() => {
    if (open) {
      untrack(() => {
        if (!unifiedToolsStore.loaded && !unifiedToolsStore.loading) unifiedToolsStore.loadTools();
        if (!triggersStore.loaded && !triggersStore.loading) triggersStore.loadTriggers();
        if (!hooksStore.loaded && !hooksStore.loading) hooksStore.loadHooks();
        if (!modelsStore.loaded && !modelsStore.loading) modelsStore.loadModels();
        if (!serverSettingsStore.loaded && !serverSettingsStore.loading) serverSettingsStore.load();
        if (!defaultToolsStore.loaded && !defaultToolsStore.loading) defaultToolsStore.load();
        if (!mcpServersStore.loaded && !mcpServersStore.loading) mcpServersStore.load();
        if (!skillsStore.installedLoaded && !skillsStore.installedLoading) skillsStore.loadInstalled();
        if (!skillsStore.enabledGlobalLoaded && !skillsStore.enabledGlobalLoading) skillsStore.loadGlobal();
        if (Object.keys(triggersStore.sources).length === 0) triggersStore.loadSources();
      });
    }
  });

  $effect(() => {
    if (open && activeTab === 'chatapp' && !chatAppLoaded) {
      untrack(() => {
        chatAppLoadError = '';
        chatAppBindingsStore.loadBindings(threadId)
          .then(() => { chatAppLoaded = true; })
          .catch((e) => { chatAppLoadError = humanizeErrorText(e, { action: 'load', resource: 'connected chat apps' }); });
      });
    }
  });

  async function loadConfig() {
    loading = true;
    try {
      const cfg = await threadConfigStore.loadConfig(threadId);
      threadConfig = cfg;
      initFormFromConfig(cfg);
    } catch {
      threadConfig = null;
      initFormFromConfig(null);
    } finally {
      loading = false;
    }
  }

  function initFormFromConfig(cfg: ThreadConfig | null) {
    instructions = cfg?.instructions ?? '';
    injectTodosInPrompt = cfg?.injectTodosInPrompt ?? false;
    showAutonomousPrompts = cfg?.showAutonomousPrompts ?? false;
    showPromptMetadata = cfg?.showPromptMetadata ?? false;
    telegramAutonomousDelivery = cfg?.telegramAutonomousDelivery ?? 'full';
    inAppNotificationLevel = cfg?.inAppNotificationLevel ?? 'notify_only';
    dreamEnabled = cfg?.dreaming?.enabled ?? false;
    dreamMinIntervalHours = String(cfg?.dreaming?.minIntervalHours ?? DREAM_DEFAULT_MIN_INTERVAL_HOURS);
    dreamMinIdleMinutes = String(cfg?.dreaming?.minIdleMinutes ?? DREAM_DEFAULT_MIN_IDLE_MINUTES);
    dreamMinTurnsSinceLast = String(cfg?.dreaming?.minTurnsSinceLast ?? DREAM_DEFAULT_MIN_TURNS_SINCE_LAST);
    dreamModel = cfg?.dreaming?.model ?? '';
    dreamStatus = '';
    hooksEnabled = cfg?.hooksEnabled ?? null;
    hookOverrides = { ...(cfg?.hookOverrides ?? {}) };
    systemPrompt = cfg?.systemPrompt ?? '';
    isCallable = cfg?.callable ?? false;
    callableName = cfg?.callableName ?? '';
    callableDescription = cfg?.callableDescription ?? '';
    imageWindowSize = cfg?.imageWindowSize != null ? String(cfg.imageWindowSize) : '';
    llmProvider = cfg?.llmConfig?.provider ?? '';
    llmProviderRoute = cfg?.llmConfig?.provider_route ?? 'default';
    llmModel = cfg?.llmConfig?.model ?? '';
    llmBaseUrl = cfg?.llmConfig?.base_url ?? '';
    llmApiKey = cfg?.llmConfig?.api_key ?? '';
    llmTemperature = cfg?.llmConfig?.temperature != null ? String(cfg.llmConfig.temperature) : '';
    llmMaxTokens = cfg?.llmConfig?.max_tokens != null ? String(cfg.llmConfig.max_tokens) : '';
    llmContextLength = cfg?.llmConfig?.context_length != null ? String(cfg.llmConfig.context_length) : '';
    llmOllamaNumCtx = cfg?.llmConfig?.ollama_num_ctx != null ? String(cfg.llmConfig.ollama_num_ctx) : '';
    llmExtendedThinking = cfg?.llmConfig?.extended_thinking != null
      ? (String(cfg.llmConfig.extended_thinking) as 'true' | 'false')
      : 'default';
    llmReasoningEffort = cfg?.llmConfig?.reasoning_effort ?? '';
    llmUseModelDefaults = cfg?.llmConfig?.use_model_defaults != null
      ? (String(cfg.llmConfig.use_model_defaults) as 'true' | 'false')
      : 'default';
    llmOpenAiApiMode = cfg?.llmConfig?.openai_api_mode ?? 'default';
    compactThresholdMode = cfg?.llmConfig?.compact_threshold_mode ?? 'default';
    compactThresholdPct = cfg?.llmConfig?.compact_threshold != null
      ? String(cfg.llmConfig.compact_threshold) : '';
    compactThresholdTokens = cfg?.llmConfig?.compact_threshold_tokens != null
      ? String(cfg.llmConfig.compact_threshold_tokens) : '';

    // Tools
    if (cfg?.hasCustomizations) {
      disabledTools = new Set(cfg.disabledTools ?? []);
      enabledTools = new Set(cfg.enabledTools ?? []);
    } else {
      disabledTools = new Set();
      enabledTools = new Set();
    }
    threadEnabledSkills = new Set(cfg?.enabledSkills ?? []);
    threadDisabledSkills = new Set(cfg?.disabledSkills ?? []);
  }

  function toggleTool(toolName: string) {
    const next = new Set(disabledTools);
    if (next.has(toolName)) { next.delete(toolName); } else { next.add(toolName); }
    disabledTools = next;
  }

  function toggleAvailableTool(toolName: string) {
    const next = new Set(enabledTools);
    if (next.has(toolName)) { next.delete(toolName); } else { next.add(toolName); }
    enabledTools = next;
  }

  function boundedInt(value: string | number, fallback: number, min: number, max: number): number {
    const parsed = parseInt(String(value ?? '').trim(), 10);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
  }

  function dreamConfigNeedsSaving(): boolean {
    return Boolean(
      threadConfig?.dreaming ||
      dreamEnabled ||
      dreamModel.trim() ||
      boundedInt(dreamMinIntervalHours, DREAM_DEFAULT_MIN_INTERVAL_HOURS, 1, 168) !== DREAM_DEFAULT_MIN_INTERVAL_HOURS ||
      boundedInt(dreamMinIdleMinutes, DREAM_DEFAULT_MIN_IDLE_MINUTES, 5, 10080) !== DREAM_DEFAULT_MIN_IDLE_MINUTES ||
      boundedInt(dreamMinTurnsSinceLast, DREAM_DEFAULT_MIN_TURNS_SINCE_LAST, 1, 10000) !== DREAM_DEFAULT_MIN_TURNS_SINCE_LAST
    );
  }

  function hasChanges(): boolean {
    const orig = threadConfig;
    const origInstructions = orig?.instructions ?? '';
    const origDisabled = new Set(orig?.disabledTools ?? []);
    const origEnabled = new Set(orig?.enabledTools ?? []);
    const origProvider = orig?.llmConfig?.provider ?? '';
    const origModel = orig?.llmConfig?.model ?? '';
    const origBaseUrl = orig?.llmConfig?.base_url ?? '';
    const origApiKey = orig?.llmConfig?.api_key ?? '';
    const origTemp = orig?.llmConfig?.temperature != null ? String(orig.llmConfig.temperature) : '';
    const origMaxTokens = orig?.llmConfig?.max_tokens != null ? String(orig.llmConfig.max_tokens) : '';
    const origContextLength = orig?.llmConfig?.context_length != null
      ? String(orig.llmConfig.context_length) : '';
    const origOllamaNumCtx = orig?.llmConfig?.ollama_num_ctx != null
      ? String(orig.llmConfig.ollama_num_ctx) : '';
    const origExtThinking = orig?.llmConfig?.extended_thinking != null
      ? String(orig.llmConfig.extended_thinking) : 'default';
    const origReasoning = orig?.llmConfig?.reasoning_effort ?? '';
    const origUseDefaults = orig?.llmConfig?.use_model_defaults != null
      ? String(orig.llmConfig.use_model_defaults) : 'default';
    const origProviderRoute = orig?.llmConfig?.provider_route ?? 'default';
    const origOpenAiApiMode = orig?.llmConfig?.openai_api_mode ?? 'default';
    const origCompactMode = orig?.llmConfig?.compact_threshold_mode ?? 'default';
    const origCompactPct = orig?.llmConfig?.compact_threshold != null
      ? String(orig.llmConfig.compact_threshold) : '';
    const origCompactTokens = orig?.llmConfig?.compact_threshold_tokens != null
      ? String(orig.llmConfig.compact_threshold_tokens) : '';
    const origSystemPrompt = orig?.systemPrompt ?? '';
    const origCallable = orig?.callable ?? false;
    const origCallableName = orig?.callableName ?? '';
    const origCallableDesc = orig?.callableDescription ?? '';
    const origImageWindow = orig?.imageWindowSize != null ? String(orig.imageWindowSize) : '';
    const origInjectTodos = orig?.injectTodosInPrompt ?? false;
    const origShowAuto = orig?.showAutonomousPrompts ?? false;
    const origShowMeta = orig?.showPromptMetadata ?? false;
    const origTelegramDelivery = orig?.telegramAutonomousDelivery ?? 'full';
    const origNotificationLevel = orig?.inAppNotificationLevel ?? 'notify_only';
    const origDream = orig?.dreaming ?? null;
    const origDreamEnabled = origDream?.enabled ?? false;
    const origDreamMinIntervalHours = String(origDream?.minIntervalHours ?? DREAM_DEFAULT_MIN_INTERVAL_HOURS);
    const origDreamMinIdleMinutes = String(origDream?.minIdleMinutes ?? DREAM_DEFAULT_MIN_IDLE_MINUTES);
    const origDreamMinTurnsSinceLast = String(origDream?.minTurnsSinceLast ?? DREAM_DEFAULT_MIN_TURNS_SINCE_LAST);
    const origDreamModel = origDream?.model ?? '';

    if (instructions !== origInstructions) return true;
    if (injectTodosInPrompt !== origInjectTodos) return true;
    if (showAutonomousPrompts !== origShowAuto) return true;
    if (showPromptMetadata !== origShowMeta) return true;
    if (telegramAutonomousDelivery !== origTelegramDelivery) return true;
    if (inAppNotificationLevel !== origNotificationLevel) return true;
    if (dreamEnabled !== origDreamEnabled) return true;
    if (String(dreamMinIntervalHours ?? '').trim() !== origDreamMinIntervalHours) return true;
    if (String(dreamMinIdleMinutes ?? '').trim() !== origDreamMinIdleMinutes) return true;
    if (String(dreamMinTurnsSinceLast ?? '').trim() !== origDreamMinTurnsSinceLast) return true;
    if (dreamModel !== origDreamModel) return true;
    if (systemPrompt !== origSystemPrompt) return true;
    if (isCallable !== origCallable) return true;
    if (callableName !== origCallableName) return true;
    if (callableDescription !== origCallableDesc) return true;
    if (String(imageWindowSize ?? '').trim() !== origImageWindow) return true;
    if (llmProvider !== origProvider) return true;
    if (llmModel !== origModel) return true;
    if (llmBaseUrl !== origBaseUrl) return true;
    if (llmApiKey !== origApiKey) return true;
    if (llmTemperature !== origTemp) return true;
    if (llmMaxTokens !== origMaxTokens) return true;
    if (llmContextLength !== origContextLength) return true;
    if (llmOllamaNumCtx !== origOllamaNumCtx) return true;
    if (llmExtendedThinking !== origExtThinking) return true;
    if (llmReasoningEffort !== origReasoning) return true;
    if (llmUseModelDefaults !== origUseDefaults) return true;
    if (llmProviderRoute !== origProviderRoute) return true;
    if (llmOpenAiApiMode !== origOpenAiApiMode) return true;
    if (compactThresholdMode !== origCompactMode) return true;
    if (compactThresholdPct !== origCompactPct) return true;
    if (compactThresholdTokens !== origCompactTokens) return true;
    if (disabledTools.size !== origDisabled.size) return true;
    for (const t of disabledTools) { if (!origDisabled.has(t)) return true; }
    if (enabledTools.size !== origEnabled.size) return true;
    for (const t of enabledTools) { if (!origEnabled.has(t)) return true; }
    const origEnabledSkills = new Set(orig?.enabledSkills ?? []);
    const origDisabledSkills = new Set(orig?.disabledSkills ?? []);
    if (threadEnabledSkills.size !== origEnabledSkills.size) return true;
    for (const skill of threadEnabledSkills) { if (!origEnabledSkills.has(skill)) return true; }
    if (threadDisabledSkills.size !== origDisabledSkills.size) return true;
    for (const skill of threadDisabledSkills) { if (!origDisabledSkills.has(skill)) return true; }
    const origHooksEnabled = orig?.hooksEnabled ?? null;
    if ((hooksEnabled ?? null) !== origHooksEnabled) return true;
    const origHookOverrides = orig?.hookOverrides ?? {};
    const origHookKeys = Object.keys(origHookOverrides);
    const curHookKeys = Object.keys(hookOverrides);
    if (origHookKeys.length !== curHookKeys.length) return true;
    for (const k of curHookKeys) { if (hookOverrides[k] !== origHookOverrides[k]) return true; }
    return false;
  }

  function checkToolCountAndSave() {
    const toolCount = effectiveToolCount;
    const callableCount = defaultToolsStore.callableThreadCount;
    if (toolCount + callableCount > 25 && !showToolWarning) {
      showToolWarning = true;
      return;
    }
    showToolWarning = false;
    handleSave();
  }

  async function handleSave() {
    saving = true;
    error = '';
    try {
      const updates: ThreadConfigUpdateRequest = {};

      // Instructions
      if (instructions.trim()) {
        updates.instructions = instructions.trim();
      } else {
        updates.clear_instructions = true;
      }

      // Disabled tools
      const defaultSet = new Set(defaultToolsStore.defaultToolNames);
      const effectiveDisabled = [...disabledTools].filter(t => defaultSet.has(t));
      if (effectiveDisabled.length > 0) {
        updates.disabled_tools = effectiveDisabled;
      } else {
        updates.clear_disabled_tools = true;
      }

      // Tools enabled for this thread (added on top of the defaults)
      if (enabledTools.size > 0) {
        updates.enabled_tools = Array.from(enabledTools);
      } else {
        updates.clear_enabled_tools = true;
      }

      // Per-thread skill overrides
      if (threadEnabledSkills.size > 0) {
        updates.enabled_skills = Array.from(threadEnabledSkills);
      } else {
        updates.clear_enabled_skills = true;
      }
      if (threadDisabledSkills.size > 0) {
        updates.disabled_skills = Array.from(threadDisabledSkills);
      } else {
        updates.clear_disabled_skills = true;
      }

      // LLM config
      const hasLlm = llmProvider || llmModel || llmTemperature || llmMaxTokens ||
        llmContextLength || llmOllamaNumCtx ||
        llmBaseUrl || llmApiKey ||
        llmExtendedThinking !== 'default' || llmReasoningEffort ||
        llmUseModelDefaults !== 'default' || llmProviderRoute !== 'default' ||
        llmOpenAiApiMode !== 'default' ||
        compactThresholdMode !== 'default' || compactThresholdPct || compactThresholdTokens;

      if (hasLlm) {
        const llm: Record<string, unknown> = {};
        llm.provider = llmProvider || null;
        llm.base_url = supportsApiMode()
          ? (llmBaseUrl || null)
          : null;
        llm.api_key = supportsApiMode()
          ? (llmApiKey || null)
          : null;
        llm.model = llmModel || null;
        llm.temperature = llmTemperature ? parseFloat(llmTemperature) : null;
        llm.max_tokens = llmMaxTokens ? parseInt(llmMaxTokens, 10) : null;
        llm.context_length = llmContextLength ? parseInt(llmContextLength, 10) : null;
        llm.ollama_num_ctx = llmOllamaNumCtx ? parseInt(llmOllamaNumCtx, 10) : null;
        if (llmExtendedThinking !== 'default') {
          llm.extended_thinking = llmExtendedThinking === 'true';
        }
        llm.reasoning_effort = llmReasoningEffort || null;
        if (llmUseModelDefaults !== 'default') {
          llm.use_model_defaults = llmUseModelDefaults === 'true';
        } else {
          llm.use_model_defaults = null;
        }
        llm.provider_route = llmProviderRoute !== 'default' ? llmProviderRoute : null;
        llm.openai_api_mode = supportsApiMode() && llmOpenAiApiMode !== 'default'
          ? llmOpenAiApiMode
          : null;
        llm.compact_threshold_mode = compactThresholdMode === 'default' ? null : compactThresholdMode;
        llm.compact_threshold = compactThresholdPct ? parseFloat(compactThresholdPct) : null;
        llm.compact_threshold_tokens = compactThresholdTokens ? parseInt(compactThresholdTokens, 10) : null;
        updates.llm_config = llm;
      } else {
        updates.clear_llm_config = true;
      }

      // System prompt
      if (systemPrompt.trim()) {
        updates.system_prompt = systemPrompt.trim();
      } else {
        updates.clear_system_prompt = true;
      }

      // Callable
      updates.callable = isCallable;
      if (isCallable) {
        updates.callable_name = callableName.trim() || null;
        updates.callable_description = callableDescription.trim() || null;
      }

      // Image window (blank inherits the model max)
      const imageWindowValue = String(imageWindowSize ?? '').trim();
      if (imageWindowValue) {
        updates.image_window_size = parseInt(imageWindowValue, 10);
      } else {
        updates.clear_image_window_size = true;
      }

      // Visibility / advanced
      updates.inject_todos_in_prompt = injectTodosInPrompt;
      updates.show_autonomous_prompts = showAutonomousPrompts;
      updates.show_prompt_metadata = showPromptMetadata;
      updates.telegram_autonomous_delivery = telegramAutonomousDelivery;
      updates.in_app_notification_level = inAppNotificationLevel;
      if (dreamConfigNeedsSaving()) {
        updates.dreaming = {
          enabled: dreamEnabled,
          min_interval_hours: boundedInt(dreamMinIntervalHours, DREAM_DEFAULT_MIN_INTERVAL_HOURS, 1, 168),
          min_idle_minutes: boundedInt(dreamMinIdleMinutes, DREAM_DEFAULT_MIN_IDLE_MINUTES, 5, 10080),
          min_turns_since_last: boundedInt(dreamMinTurnsSinceLast, DREAM_DEFAULT_MIN_TURNS_SINCE_LAST, 1, 10000),
          model: dreamModel.trim() || null,
        };
      } else {
        updates.clear_dreaming = true;
      }

      // Lifecycle-hook per-thread enablement.
      if (hooksEnabled === null) {
        updates.clear_hooks_enabled = true;
      } else {
        updates.hooks_enabled = hooksEnabled;
      }
      if (Object.keys(hookOverrides).length > 0) {
        updates.hook_overrides = { ...hookOverrides };
      } else {
        updates.clear_hook_overrides = true;
      }

      const result = await threadConfigStore.updateConfig(threadId, updates);

      // Sync sidebar title to callable name
      if (isCallable && callableName.trim()) {
        threadsStore.applyBackendTitle(threadId, callableName.trim());
      }
      const thread = threadsStore.threads.find((item) => item.id === threadId);
      threadsStore.updateThread(threadId, {
        callable: result.callable,
        platform: platformAfterCallableChange(threadId, thread?.platform, result.callable),
      });

      // Refresh context stats
      api.getThreadContextStats(threadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === threadId) {
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats.model);
        }
      });

      onClose();
    } catch (e) {
      error = humanizeErrorText(e, { action: 'save', resource: 'thread settings' });
    } finally {
      saving = false;
    }
  }

  async function handleRunDream() {
    dreamRunning = true;
    dreamStatus = '';
    error = '';
    try {
      const result = await api.triggerThreadDream(threadId, {
        model: dreamModel.trim() || null,
      });
      dreamStatus = `Started ${result.shadow_thread_id}`;
      await threadsStore.syncFromBackend();
    } catch (e) {
      error = humanizeErrorText(e, { action: 'start', resource: 'dreaming' });
    } finally {
      dreamRunning = false;
    }
  }

  async function handleReset() {
    saving = true;
    error = '';
    try {
      await threadConfigStore.deleteConfig(threadId);
      initFormFromConfig(null);
      const thread = threadsStore.threads.find((item) => item.id === threadId);
      threadsStore.updateThread(threadId, {
        callable: false,
        platform: platformAfterCallableChange(threadId, thread?.platform, false),
      });

      api.getThreadContextStats(threadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === threadId) {
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats.model);
        }
      });

      onClose();
    } catch (e) {
      error = humanizeErrorText(e, { action: 'reset', resource: 'thread settings' });
    } finally {
      saving = false;
    }
  }
</script>

{#snippet authBadge(status: string | null | undefined, provider: string | null | undefined)}
  {#if status === 'needs_setup'}
    <span class="auth-badge needs-setup" title={`Provider "${provider}": no credential saved`}>auth required</span>
  {:else if status === 'pending'}
    <span class="auth-badge pending" title={`Provider "${provider}": credential setup pending`}>auth pending</span>
  {/if}
{/snippet}

{#if open}
  <div class="thread-settings-modal">
    <div class="settings-header">
      <button class="back-btn" onclick={onClose}>
        <Icon name="chevronLeft" size={22} />
      </button>
      <h2>Thread Settings</h2>
    </div>

    <div class="tab-bar">
      <button class="tab-btn" class:active={activeTab === 'instructions'} aria-current={activeTab === 'instructions' ? 'page' : undefined} onclick={() => (activeTab = 'instructions')}>
        Instructions
      </button>
      <button class="tab-btn" class:active={activeTab === 'system'} aria-current={activeTab === 'system' ? 'page' : undefined} onclick={() => (activeTab = 'system')}>
        System
        {#if systemPrompt.trim()}<span class="tab-badge">1</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'agent'} aria-current={activeTab === 'agent' ? 'page' : undefined} onclick={() => (activeTab = 'agent')}>
        Agent
        {#if isCallable}<span class="tab-badge">1</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'dream'} aria-current={activeTab === 'dream' ? 'page' : undefined} onclick={() => (activeTab = 'dream')}>
        Dream
        {#if dreamEnabled}<span class="tab-badge">1</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'hooks'} aria-current={activeTab === 'hooks' ? 'page' : undefined} onclick={() => (activeTab = 'hooks')}>
        Hooks
        {#if hooksEnabled !== null || hookOverrideCount > 0}<span class="tab-badge">{hookOverrideCount || '•'}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'model'} aria-current={activeTab === 'model' ? 'page' : undefined} onclick={() => (activeTab = 'model')}>
        Model
      </button>
      <button class="tab-btn" class:active={activeTab === 'tools'} aria-current={activeTab === 'tools' ? 'page' : undefined} onclick={() => (activeTab = 'tools')}>
        Tools
        {#if disabledToolCount > 0}<span class="tab-badge">{disabledToolCount}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'mcp'} aria-current={activeTab === 'mcp' ? 'page' : undefined} onclick={() => (activeTab = 'mcp')}>
        MCP
        {#if mcpOverrideCount > 0}<span class="tab-badge">{mcpOverrideCount}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'skills'} aria-current={activeTab === 'skills' ? 'page' : undefined} onclick={() => (activeTab = 'skills')}>
        Skills
        {#if resolvedActiveSkillNames.size > 0}<span class="tab-badge">{resolvedActiveSkillNames.size}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'triggers'} aria-current={activeTab === 'triggers' ? 'page' : undefined} onclick={() => (activeTab = 'triggers')}>
        Triggers
        {#if activeTriggerCount > 0}<span class="tab-badge">{activeTriggerCount}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'chatapp'} aria-current={activeTab === 'chatapp' ? 'page' : undefined} onclick={() => (activeTab = 'chatapp')}>
        Chat App
        {#if chatAppBindings.length > 0}<span class="tab-badge">{chatAppBindings.length}</span>{/if}
      </button>
    </div>

    <div class="settings-body">
      {#if loading}
        <div class="loading-state"><InlineLoader text="Loading thread settings…" /></div>

      {:else if activeTab === 'instructions'}
        <div class="setting-group">
          <label class="setting-label">Custom Instructions</label>
          <p class="hint">Appended to the system prompt for this thread only.</p>
          <textarea
            class="setting-textarea"
            bind:value={instructions}
            placeholder="e.g. Focus on email management. Be concise."
            maxlength={5000}
            rows={6}
          ></textarea>
          <span class="char-count">{instructions.length} / 5,000</span>
        </div>

        <div class="section-divider">
          <span class="section-heading">Advanced</span>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={injectTodosInPrompt} />
            <span>Inject tasks into system prompt</span>
          </label>
          <p class="hint">Include active tasks in the system prompt so the LLM sees them without tool calls.</p>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={showAutonomousPrompts} />
            <span>Force show autonomous prompts on this thread</span>
          </label>
          <p class="hint">
            Per-thread override. When the global "Show autonomous prompts"
            setting is off, enable this to still show scheduler, watchdog, and
            trigger prompts on this specific thread. No effect when the global
            setting is on.
          </p>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={showPromptMetadata} />
            <span>Show prompt metadata</span>
          </label>
          <p class="hint">Show time context and trigger type prepended to each message.</p>
        </div>

      {:else if activeTab === 'system'}
        <div class="setting-group">
          <label class="setting-label">Custom System Prompt</label>
          <p class="hint">Replaces the base system prompt for this thread. Leave empty for default.</p>
          <textarea
            class="setting-textarea mono"
            bind:value={systemPrompt}
            placeholder="You are a specialized assistant that…"
            maxlength={50000}
            rows={10}
          ></textarea>
          <span class="char-count">{systemPrompt.length} / 50,000</span>
        </div>

      {:else if activeTab === 'agent'}
        <div class="section-divider">
          <span class="section-heading">Agent Configuration</span>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={isCallable} />
            <span>Make Callable</span>
          </label>
          <p class="hint">Mark this thread as a callable sub-agent.</p>
        </div>
        {#if isCallable}
          <div class="setting-group">
            <label class="setting-label">Callable Name</label>
            <input
              type="text"
              class="setting-input"
              bind:value={callableName}
              placeholder="e.g. ResearchAgent"
              maxlength={64}
            />
            <span class="char-count">{callableName.length} / 64</span>
          </div>
          <div class="setting-group">
            <label class="setting-label">Callable Description</label>
            <textarea
              class="setting-textarea"
              bind:value={callableDescription}
              placeholder="Describe what this agent does…"
              maxlength={500}
              rows={3}
            ></textarea>
            <span class="char-count">{callableDescription.length} / 500</span>
          </div>
        {/if}

        <div class="section-divider">
          <span class="section-heading">Image Window</span>
          <p class="hint">
            How many images stay visible to the model at once. Newest kept; older drop out of context (still on disk for file_read).
          </p>
        </div>
        <div class="setting-group">
          <label class="setting-label" for="mobile-image-window">Max images in context</label>
          <input
            id="mobile-image-window"
            type="number"
            class="setting-input"
            min="1"
            max="3000"
            step="1"
            bind:value={imageWindowSize}
            placeholder="Model maximum"
          />
          <p class="hint">Leave blank to keep up to the model's maximum. Lower it to cap image token cost.</p>
        </div>

      {:else if activeTab === 'dream'}
        <div class="section-divider">
          <span class="section-heading">Dreaming</span>
          <p class="hint">
            Background self-reflection for this thread. Dream turns run in a shadow thread and write only through the dream tool policy.
          </p>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={dreamEnabled} />
            <span>Enable Dreaming</span>
          </label>
        </div>
        <div class="dream-grid">
          <div class="setting-group">
            <label class="setting-label" for="mobile-dream-min-interval">Min Interval Hours</label>
            <input
              id="mobile-dream-min-interval"
              type="number"
              class="setting-input"
              min="1"
              max="168"
              step="1"
              bind:value={dreamMinIntervalHours}
            />
          </div>
          <div class="setting-group">
            <label class="setting-label" for="mobile-dream-min-idle">Min Idle Minutes</label>
            <input
              id="mobile-dream-min-idle"
              type="number"
              class="setting-input"
              min="5"
              max="10080"
              step="5"
              bind:value={dreamMinIdleMinutes}
            />
          </div>
          <div class="setting-group">
            <label class="setting-label" for="mobile-dream-min-turns">Min Turns Since Last</label>
            <input
              id="mobile-dream-min-turns"
              type="number"
              class="setting-input"
              min="1"
              max="10000"
              step="1"
              bind:value={dreamMinTurnsSinceLast}
            />
          </div>
          <div class="setting-group">
            <label class="setting-label" for="mobile-dream-model">Dream Model</label>
            <input
              id="mobile-dream-model"
              type="text"
              class="setting-input"
              bind:value={dreamModel}
              placeholder="Default model"
              maxlength={120}
            />
          </div>
        </div>

        {#if threadConfig?.dreaming?.lastDreamAt}
          <p class="hint">Last dream: {new Date(threadConfig.dreaming.lastDreamAt).toLocaleString()}</p>
        {/if}

        <div class="dream-actions">
          <button
            class="action-btn"
            type="button"
            onclick={handleRunDream}
            disabled={saving || dreamRunning || !dreamEnabled || hasChanges()}
            title={hasChanges() ? 'Save changes before running a dream' : 'Run dream now'}
          >
            {dreamRunning ? 'Starting…' : 'Run Dream'}
          </button>
          {#if dreamStatus}
            <span class="dream-status">{dreamStatus}</span>
          {/if}
        </div>

      {:else if activeTab === 'hooks'}
        <div class="section-divider">
          <span class="section-heading">Hooks</span>
          <p class="hint">
            Control which lifecycle hooks run on this thread. Create and edit hooks from the dashboard Hooks panel or the /hook command.
          </p>
        </div>
        <div class="setting-group">
          <span class="setting-label">Hooks on this thread</span>
          <div class="hook-seg" role="group" aria-label="Hooks master switch">
            <button class="hseg" class:active={hooksMasterValue === 'inherit'} type="button" onclick={() => setHooksMaster('inherit')}>Inherit</button>
            <button class="hseg" class:active={hooksMasterValue === 'on'} type="button" onclick={() => setHooksMaster('on')}>On</button>
            <button class="hseg" class:active={hooksMasterValue === 'off'} type="button" onclick={() => setHooksMaster('off')}>Off</button>
          </div>
          <p class="hint">Inherit follows the global hooks setting. On / Off force hooks for this thread.</p>
        </div>

        {#if threadHooks.length === 0}
          <p class="hint">No hooks apply to this thread yet. Global hooks and hooks created for this thread will appear here.</p>
        {:else}
          <div class="hook-ovr-list" class:dimmed={hooksMasterValue === 'off'}>
            {#each hookGroups as group (group.meta.key)}
              <div class="hook-cat">
                <div class="hook-cat-head">
                  <Icon name={group.meta.icon} size={12} />
                  <span>{group.meta.label}</span>
                </div>
                {#each group.hooks as hook (hook.id)}
                  <div class="hook-ovr-row">
                    <div class="hook-ovr-meta">
                      <span class="hook-ovr-name">{hook.name}</span>
                      <span class="hook-ovr-sub">{HOOK_EVENT_META[hook.event].label} · {HOOK_ACTION_META[hook.action].label}{hook.scope === 'global' ? ' · Global' : ''}</span>
                    </div>
                    <div class="hook-seg small" role="group" aria-label="Override for {hook.name}">
                      <button class="hseg" class:active={hookOverrideValue(hook.id) === 'default'} type="button" onclick={() => setHookOverride(hook.id, 'default')}>Default</button>
                      <button class="hseg" class:active={hookOverrideValue(hook.id) === 'on'} type="button" onclick={() => setHookOverride(hook.id, 'on')}>On</button>
                      <button class="hseg" class:active={hookOverrideValue(hook.id) === 'off'} type="button" onclick={() => setHookOverride(hook.id, 'off')}>Off</button>
                    </div>
                  </div>
                {/each}
              </div>
            {/each}
          </div>
          <p class="hint">Default uses the hook's own state. On / Off override it for this thread only.</p>
        {/if}

      {:else if activeTab === 'model'}
        <div class="setting-group">
          <label class="setting-label" for="mobile-thread-provider">Provider</label>
          <ProviderSelect
            id="mobile-thread-provider"
            bind:value={llmProvider}
            groups={mobileThreadProviderGroups}
            includeDefault={true}
            defaultLabel="Default (inherit global)"
            defaultDescription="Use the provider configured in global Settings."
          />
        </div>

        {#if showProviderRouteSelect()}
          <div class="setting-group">
            <label class="setting-label" for="thread-provider-route">Provider Route</label>
            <select id="thread-provider-route" class="setting-input" bind:value={llmProviderRoute}>
              <option value="default">Default (inherit global)</option>
              {#each supportedRoutesForProvider(getEffectiveProvider(), providerCatalog) as route}
                <option value={route}>{providerRouteLabel(route)}</option>
              {/each}
            </select>
            <p class="hint">Current route: {providerRouteLabel(selectedRoute())}</p>
          </div>
        {/if}

        {#if showAnthropicRouteNudge()}
          <div class="route-nudge">
            <p class="route-nudge-text">
              This gateway's OpenAI-compatible path drops Claude's reasoning (thinking) signatures, so multi-turn reasoning passback breaks. Switch this thread to the Anthropic Messages route for native thinking via the gateway's /v1/messages endpoint.
            </p>
            <button
              type="button"
              class="route-nudge-button"
              onclick={() => (llmProviderRoute = 'anthropic_messages')}
            >
              Use Anthropic Messages route
            </button>
          </div>
        {/if}

        {#if supportsApiMode()}
          <div class="setting-group">
            <label class="setting-label" for="llm-openai-api-mode">API Mode</label>
            <select id="llm-openai-api-mode" class="setting-input" bind:value={llmOpenAiApiMode}>
              <option value="default">Default (inherit global)</option>
              <option value="chat_completions">Chat Completions (not recommended if thinking is enabled)</option>
              <option value="responses">Responses API</option>
            </select>
            <p class="hint">Responses API is the default for OpenAI-compatible reasoning models and OpenRouter beta. Chat Completions remains available as a compatibility override.</p>
          </div>
        {/if}

        {#if supportsConnectionOverride()}
          <div class="setting-group">
            <label class="setting-label" for="llm-base-url">API Base URL</label>
            <input
              id="llm-base-url"
              type="text"
              class="setting-input"
              bind:value={llmBaseUrl}
              placeholder="http://cli-proxy-api-latest:8317/v1"
            />
            <p class="hint">OpenAI-compatible endpoint reachable by the Nymeria backend. Leave empty to inherit global settings.</p>
          </div>

          <div class="setting-group">
            <label class="setting-label" for="llm-api-key">API Key</label>
            <input
              id="llm-api-key"
              type="password"
              class="setting-input"
              bind:value={llmApiKey}
              placeholder="Leave empty to inherit global key"
              autocomplete="off"
            />
          </div>
        {/if}

        <div class="setting-group">
          <label class="setting-label">Model</label>
          {#if availableModelsState.models.length > 0}
            <select class="setting-input" bind:value={llmModel}>
              <option value="">Default (inherit global)</option>
              {#each availableModelsState.models as model}
                <option value={model.id}>{model.name || model.id}</option>
              {/each}
            </select>
            <p class="hint">{availableModelsState.models.length} models available from provider</p>
          {:else if availableModelsState.loading}
            <select class="setting-input" disabled>
              <option>Loading models…</option>
            </select>
            <p class="hint">Fetching available models from provider…</p>
          {/if}
          <input
            type="text"
            class="setting-input"
            bind:value={llmModel}
            placeholder="Leave empty for global default"
          />
          <p class="hint">Model lists come from the provider's models endpoint when available. You can still enter an exact model ID manually.</p>
          {#if threadModelMeta && llmModel}
            <div class="model-meta">
              <span class="meta-name">{threadModelMeta.name}</span>
              <span class="meta-details">
                {modelsStore.formatContext(threadModelMeta.context_length)} ctx
                {#if threadModelMeta.pricing_prompt != null}
                  &middot; In: {modelsStore.formatPrice(threadModelMeta.pricing_prompt)}
                {/if}
                {#if threadModelMeta.pricing_completion != null}
                  &middot; Out: {modelsStore.formatPrice(threadModelMeta.pricing_completion)}
                {/if}
                {#if threadModelMeta.input_modalities.includes('image')}
                  &middot; Vision
                {/if}
              </span>
            </div>
          {/if}
          {#if fastTierRef || smartTierRef}
            <div class="tier-quickpick">
              <span class="hint">Quick tier:</span>
              {#if fastTierRef}
                <button type="button" class="tier-btn" onclick={() => applyTier('fast')} title={`Fast tier: ${fastTierRef}`}>Fast</button>
              {/if}
              {#if smartTierRef}
                <button type="button" class="tier-btn" onclick={() => applyTier('smart')} title={`Smart tier: ${smartTierRef}`}>Smart</button>
              {/if}
            </div>
          {/if}
        </div>

        <div class="setting-group">
          <label class="setting-label">Use Model Defaults</label>
          <select class="setting-input" bind:value={llmUseModelDefaults}>
            <option value="default">Default (inherit global)</option>
            <option value="true">On</option>
            <option value="false">Off</option>
          </select>
          <p class="hint">
            Let provider apply optimal defaults for temp, top_p, etc.
            {#if llmUseModelDefaults === 'true' && threadModelMeta?.default_temperature != null}
              (temp: {threadModelMeta.default_temperature})
            {/if}
          </p>
        </div>

        <div class="setting-group">
          <label class="setting-label">Temperature</label>
          <input
            type="number"
            class="setting-input"
            min="0"
            max="2"
            step="0.1"
            bind:value={llmTemperature}
            placeholder="Default"
            disabled={llmUseModelDefaults === 'true'}
          />
        </div>

        <div class="setting-group">
          <label class="setting-label">Max Output Tokens</label>
          <input
            type="number"
            class="setting-input"
            min="1"
            max="128000"
            step="1"
            bind:value={llmMaxTokens}
            placeholder="Default"
          />
        </div>

        <div class="setting-group">
          <label class="setting-label" for="thread-llm-context-length">Context Window Tokens</label>
          <input
            id="thread-llm-context-length"
            type="number"
            class="setting-input"
            min="1000"
            max="2000000"
            step="1"
            bind:value={llmContextLength}
            placeholder="Default"
          />
          <p class="hint">Manual local-model context override. Leave empty to inherit global or auto-detected metadata.</p>
        </div>

        <div class="setting-group">
          <label class="setting-label" for="thread-llm-ollama-num-ctx">Ollama num_ctx</label>
          <input
            id="thread-llm-ollama-num-ctx"
            type="number"
            class="setting-input"
            min="1000"
            max="2000000"
            step="1"
            bind:value={llmOllamaNumCtx}
            placeholder="Default"
          />
          <p class="hint">Per-thread Ollama options.num_ctx override. Leave empty to inherit global or auto-detect.</p>
        </div>

        <div class="setting-group">
          <label class="setting-label">Extended Thinking</label>
          <select class="setting-input" bind:value={llmExtendedThinking}>
            <option value="default">Default (inherit global)</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </select>
        </div>

        <div class="setting-group">
          <label class="setting-label">Reasoning Effort</label>
          <select class="setting-input" bind:value={llmReasoningEffort}>
            <option value="">Default (inherit global)</option>
            {#each REASONING_EFFORT_LEVELS as level (level)}
              {@const unsupported = effortOptionDisabled(level, effortSet, llmReasoningEffort)}
              <option value={level} disabled={unsupported}>
                {reasoningEffortLabel(level)}{unsupported ? ' (not supported)' : ''}
              </option>
            {/each}
          </select>
          {#if showEffortClampHint}
            <div class="effort-clamp-note">
              This model supports up to {reasoningEffortLabel(effortClampMax)}. Higher settings are reduced automatically.
            </div>
          {/if}
        </div>

        <div class="setting-group">
          <label class="setting-label" for="thread-compact-mode">Auto-Compact Trigger</label>
          <select id="thread-compact-mode" class="setting-input" bind:value={compactThresholdMode}>
            <option value="default">Default (inherit global)</option>
            <option value="percentage">Percentage of context window</option>
            <option value="tokens">Absolute input-token count</option>
          </select>
        </div>

        {#if compactThresholdMode === 'percentage'}
          <div class="setting-group">
            <label class="setting-label" for="thread-compact-pct">Compact Threshold (0.05 - 0.95)</label>
            <input
              id="thread-compact-pct"
              class="setting-input"
              type="number"
              min="0.05"
              max="0.95"
              step="0.01"
              bind:value={compactThresholdPct}
              placeholder="Leave empty to inherit global"
            />
          </div>
        {:else if compactThresholdMode === 'tokens'}
          <div class="setting-group">
            <label class="setting-label" for="thread-compact-tokens">Compact Token Threshold (1,000 - 2,000,000)</label>
            <input
              id="thread-compact-tokens"
              class="setting-input"
              type="number"
              min="1000"
              max="2000000"
              step="1000"
              bind:value={compactThresholdTokens}
              placeholder="Leave empty to inherit global"
            />
          </div>
        {/if}

      {:else if activeTab === 'tools'}
        <div class="tools-search">
          <input
            type="text"
            class="setting-input"
            bind:value={toolSearch}
            placeholder="Search tools…"
          />
        </div>

        {#if toolsLoadError}
          <div class="loading-state">{toolsLoadError}</div>
        {:else if toolsLoading}
          <div class="loading-state"><InlineLoader text="Loading tools…" /></div>
        {:else}
          <div class="tools-list">
            {#if filteredTools.length === 0}
              <div class="loading-state">
                {toolSearch.trim() ? 'No default tools match your search.' : 'No default tools yet.'}
              </div>
            {:else}
              {#each filteredTools as tool (tool.id)}
                <div class="tool-row" class:tool-disabled={disabledTools.has(tool.name)}>
                  <div class="tool-info">
                    <span class="tool-name">{tool.name}{@render authBadge(tool.authStatus, tool.authProvider)}</span>
                    <span class="tool-desc">{tool.description}</span>
                  </div>
                  <button
                    class="toggle-btn"
                    class:off={disabledTools.has(tool.name)}
                    onclick={() => toggleTool(tool.name)}
                    type="button"
                  >
                    <span class="toggle-track"><span class="toggle-thumb"></span></span>
                  </button>
                </div>
              {/each}
            {/if}
          </div>

          {#if availableTools.length > 0}
            <div class="section-divider">
              <span class="section-heading">
                Available to add
                {#if enabledToolCount > 0}<span class="tab-badge">{enabledToolCount}</span>{/if}
              </span>
              <p class="hint">Not in your defaults. Enable for this thread only.</p>
            </div>
            <div class="tools-list">
              {#if filteredAvailableTools.length === 0}
                <div class="loading-state">
                  {toolSearch.trim() ? 'Nothing matches your search.' : 'No tools available to add.'}
                </div>
              {:else}
                {#each filteredAvailableTools as tool (tool.name)}
                  <div class="tool-row" class:tool-enabled={enabledTools.has(tool.name)}>
                    <div class="tool-info">
                      <span class="tool-name">{tool.name}{@render authBadge(tool.authStatus, tool.authProvider)}</span>
                      <span class="tool-desc">{tool.description}</span>
                    </div>
                    <button
                      class="toggle-btn"
                      class:off={!enabledTools.has(tool.name)}
                      onclick={() => toggleAvailableTool(tool.name)}
                      type="button"
                    >
                      <span class="toggle-track"><span class="toggle-thumb"></span></span>
                    </button>
                  </div>
                {/each}
              {/if}
            </div>
          {/if}

        {/if}

        <p class="hint">
          Changing which tools are bound to this thread re-primes the prompt cache
          on the next turn (a one-time cost).
        </p>

      {:else if activeTab === 'mcp'}
        {#if toolsLoadError}
          <div class="loading-state">{toolsLoadError}</div>
        {:else if toolsLoading}
          <div class="loading-state"><InlineLoader text="Loading MCP tools…" /></div>
        {:else}
          <div class="section-divider">
            <span class="section-heading">
              <Icon name="terminal" size={14} />
              MCP Servers
            </span>
            <p class="hint">Tools from MCP servers. Enable for this thread.</p>
          </div>

          {#if mcpServersForThread.length === 0}
            <div class="loading-state">No MCP servers installed. Install one in Settings → MCP.</div>
          {:else}
            {#each mcpServersForThread as server (server.id)}
              <div class="mcp-server-group">
                <div class="mcp-server-header-row">
                  <button
                    class="mcp-server-header"
                    onclick={() => expandedMcpServer = expandedMcpServer === server.id ? null : server.id}
                  >
                    <span class="mcp-server-name">{server.name}</span>
                    <span class="mcp-tool-count">{server.tools.filter(isMcpThreadToolEnabled).length}/{server.tools.length}</span>
                  </button>
                  <button
                    class="mcp-bulk-btn"
                    type="button"
                    onclick={() => toggleAllMcpTools(server.tools, !areMcpToolsAllEnabled(server.tools))}
                  >
                    {areMcpToolsAllEnabled(server.tools) ? 'Disable all' : 'Enable all'}
                  </button>
                </div>
                {#if expandedMcpServer === server.id}
                  <div class="mcp-tool-list">
                    {#if server.tools.length === 0}
                      <div class="loading-state">No tools discovered for this server.</div>
                    {:else}
                      {#each server.tools as tool (tool.name)}
                        {@const isEnabled = isMcpThreadToolEnabled(tool)}
                        <div class="tool-row" class:tool-enabled={isEnabled} class:tool-disabled={!isEnabled && tool.isDefault}>
                          <div class="tool-info">
                            <span class="tool-name">{tool.shortName}</span>
                            <span class="tool-desc">{tool.description}</span>
                          </div>
                          <button
                            class="toggle-btn"
                            class:off={!isEnabled}
                            onclick={() => toggleMcpThreadTool(tool)}
                            type="button"
                            aria-label={isEnabled ? 'Disable for this thread' : 'Enable for this thread'}
                            aria-pressed={isEnabled}
                          >
                            <span class="toggle-track"><span class="toggle-thumb"></span></span>
                          </button>
                        </div>
                      {/each}
                    {/if}
                  </div>
                {/if}
              </div>
            {/each}
          {/if}

          {#if showMcpAddForm}
            <MCPServerForm
              mode="add"
              loading={mcpAddLoading}
              error={mcpAddError}
              onSubmit={handleMcpAdd}
              onCancel={() => { showMcpAddForm = false; mcpAddError = null; }}
            />
          {:else}
            <button class="mcp-add-btn" onclick={() => showMcpAddForm = true}>
              <Icon name="plus" size={14} /> Add MCP Server
            </button>
          {/if}
        {/if}

      {:else if activeTab === 'skills'}
        {#if skillsStore.installedError}
          <div class="loading-state">{skillsStore.installedError}</div>
        {:else if skillsStore.installedLoading && !skillsStore.installedLoaded}
          <div class="loading-state"><InlineLoader text="Loading skills…" /></div>
        {:else if skillsStore.installed.length === 0}
          <div class="loading-state">No skills installed.</div>
        {:else}
          {#if skillsStore.enabledGlobalError}
            <div class="loading-state">{skillsStore.enabledGlobalError}</div>
          {/if}
          <div class="section-divider">
            <span class="section-heading">Thread Skills</span>
            <p class="hint">Enable or disable installed skills for this thread. Globally enabled and default skills are active unless disabled here.</p>
          </div>

          <div class="skills-list">
            {#each skillsStore.installed as skill (skill.name)}
              {@const defaultOn = skill.default_active}
              {@const globalOn = skillsStore.enabledGlobal.includes(skill.name)}
              {@const threadOn = threadEnabledSkills.has(skill.name)}
              {@const threadOff = threadDisabledSkills.has(skill.name)}
              {@const activeHere = isSkillActiveHere(skill)}
              <div class="skill-row" class:skill-active={activeHere} class:skill-disabled={threadOff}>
                <div class="skill-info">
                  <div class="skill-head">
                    <span class="skill-name">{skill.name}</span>
                    <span class="skill-scope">{skill.scope}</span>
                    {#if defaultOn}<span class="skill-chip">default</span>{/if}
                    {#if globalOn}<span class="skill-chip">global</span>{/if}
                    {#if skill.is_skill_kit}<span class="skill-chip">Skill Kit</span>{/if}
                  </div>
                  <p class="skill-desc">{skill.description}</p>
                  {#if skill.required_tools.length > 0}
                    <p class="skill-tools">
                      Requires {skill.required_tools.join(', ')}
                    </p>
                  {/if}
                </div>
                <div class="skill-actions">
                  <button
                    class="skill-btn"
                    class:skill-btn-danger={threadOff}
                    onclick={() => toggleThreadSkillDisabled(skill.name)}
                    type="button"
                  >
                    {threadOff ? 'Disabled' : 'Disable'}
                  </button>
                  <button
                    class="skill-btn"
                    class:skill-btn-active={threadOn}
                    onclick={() => toggleThreadSkillEnabled(skill.name)}
                    type="button"
                  >
                    {threadOn ? 'Thread' : 'Enable'}
                  </button>
                </div>
              </div>
            {/each}
          </div>
        {/if}

      {:else if activeTab === 'triggers'}
        <TriggerConfigTab {threadId} />

      {:else if activeTab === 'chatapp'}
        <p class="hint" style="margin-bottom: var(--spacing-sm);">
          Connect a Telegram chat to this thread so messages flow both ways.
        </p>

        <div class="setting-group">
          <label class="setting-label" for="telegram-autonomous-delivery">
            Telegram autonomous output
          </label>
          <select
            id="telegram-autonomous-delivery"
            class="setting-input"
            bind:value={telegramAutonomousDelivery}
          >
            <option value="full">Full output</option>
            <option value="notify_only">Notify only</option>
            <option value="off">Off</option>
          </select>
        </div>
        <div class="setting-group">
          <label class="setting-label" for="in-app-notification-level">
            Notification center
          </label>
          <select
            id="in-app-notification-level"
            class="setting-input"
            bind:value={inAppNotificationLevel}
          >
            <option value="notify_only">Notify only</option>
            <option value="all_autonomous">All autonomous completions</option>
            <option value="off">Off</option>
          </select>
        </div>

        {#if chatAppLoadError}
          <div class="error-bar">{chatAppLoadError}</div>
        {/if}

        {#if chatAppBindings.length === 0}
          <div class="chatapp-cta">
            <p class="hint">No chat app is connected to this thread yet.</p>
            <div class="chatapp-cta-buttons">
              <button
                class="action-btn"
                type="button"
                onclick={() => { showChatAppWizard = true; }}
              >Connect via shared bot</button>
              <button
                class="action-btn"
                type="button"
                onclick={() => { showMyBotWizard = true; }}
              >Use my own bot</button>
            </div>
            <p class="hint" style="font-size: var(--font-size-xs);">
              The shared bot uses Nymeria's global Telegram bot. "Use my own bot"
              lets you register a BotFather token for a private bot.
            </p>
          </div>
        {:else}
          <ul class="binding-list">
            {#each chatAppBindings as binding (binding.id)}
              <li class="binding-row">
                <div class="binding-info">
                  <span class="binding-provider">{binding.provider}</span>
                  <code class="binding-chat">{binding.platform_chat_id}</code>
                  <span class="binding-type">
                    {binding.user_telegram_bot_id ? 'Your bot' : 'Shared bot'}
                  </span>
                  <span class="binding-when">
                    {new Date(binding.created_at).toLocaleDateString()}
                  </span>
                </div>
                <button
                  class="action-btn danger"
                  type="button"
                  onclick={() => handleUnbindChatApp(binding.id)}
                >Unbind</button>
              </li>
            {/each}
          </ul>
          <p class="hint">Only one chat per thread at a time.</p>
        {/if}
      {/if}
    </div>

    {#if error}
      <div class="error-bar">{error}</div>
    {/if}

    <div class="settings-footer">
      <button class="footer-btn reset" onclick={handleReset} disabled={saving} type="button">
        Reset
      </button>
      <div class="footer-right">
        <button class="footer-btn secondary" onclick={onClose} disabled={saving} type="button">
          Cancel
        </button>
        <button
          class="footer-btn primary"
          onclick={checkToolCountAndSave}
          disabled={saving || !hasChanges()}
          type="button"
        >
          {saving ? 'Saving…' : 'Save Changes'}
        </button>
      </div>
    </div>
  </div>

  {#if showToolWarning}
    <ToolCountWarning
      toolCount={effectiveToolCount}
      callableCount={defaultToolsStore.callableThreadCount}
      onContinue={() => { showToolWarning = false; handleSave(); }}
      onGoBack={() => { showToolWarning = false; }}
    />
  {/if}

  {#if showChatAppWizard}
    <ConnectTelegramWizard
      {threadId}
      onClose={() => { showChatAppWizard = false; }}
      onBound={() => { chatAppBindingsStore.loadBindings(threadId); threadsStore.syncFromBackend(); }}
    />
  {/if}

  {#if showMyBotWizard}
    <ConnectMyTelegramBotWizard
      {threadId}
      onClose={() => { showMyBotWizard = false; }}
      onBound={() => { chatAppBindingsStore.loadBindings(threadId); threadsStore.syncFromBackend(); }}
    />
  {/if}
{/if}

<style>
  .thread-settings-modal {
    position: fixed;
    inset: 0;
    z-index: 300;
    background: var(--bg-base);
    display: flex;
    flex-direction: column;
  }

  .settings-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-md);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    padding-top: var(--safe-area-top);
  }

  .back-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
  }

  .back-btn:active {
    background: var(--bg-hover);
  }

  .settings-header h2 {
    flex: 1;
    font-size: var(--font-size-lg);
    font-weight: 600;
  }

  .tab-bar {
    display: flex;
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    overflow-x: auto;
  }

  .tab-btn {
    flex: 1;
    min-width: 0;
    padding: var(--spacing-sm) var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    white-space: nowrap;
    min-height: var(--touch-target-min);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
  }

  .tab-btn.active {
    color: var(--accent-primary);
    border-bottom-color: var(--accent-primary);
  }

  .tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 4px;
    font-size: 10px;
    font-weight: 600;
    background: var(--accent-primary);
    color: var(--text-on-accent);
    border-radius: 9px;
  }

  .settings-body {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-lg);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .loading-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-xl);
  }

  .setting-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .dream-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: var(--spacing-md);
  }

  .dream-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .dream-actions .action-btn:disabled {
    opacity: 0.45;
  }

  .dream-status {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .setting-label {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .setting-input {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: 16px;
    min-height: var(--touch-target-min);
  }

  .setting-input:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .setting-input:disabled {
    opacity: 0.4;
  }

  select.setting-input {
    appearance: none;
    -webkit-appearance: none;
  }

  .setting-textarea {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: 16px;
    resize: vertical;
    font-family: inherit;
    line-height: 1.5;
  }

  .setting-textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .setting-textarea.mono {
    font-family: monospace;
    font-size: 14px;
  }

  .setting-toggle {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    min-height: var(--touch-target-min);
  }

  .setting-toggle input {
    width: 22px;
    height: 22px;
    accent-color: var(--accent-primary);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .route-nudge {
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--error);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--error) 8%, transparent);
  }
  .route-nudge-text {
    margin: 0 0 var(--spacing-sm) 0;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    line-height: 1.4;
  }
  .route-nudge-button {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .tier-quickpick {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
  }
  .tier-quickpick .hint {
    margin-top: 0;
  }
  .tier-btn {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .effort-clamp-note {
    margin-top: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--warning);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--warning) 8%, transparent);
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    line-height: 1.4;
  }

  .char-count {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-align: right;
  }

  .section-divider {
    padding-top: var(--spacing-md);
    margin-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
  }

  .section-heading {
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  /* Model metadata */
  .model-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    margin-top: var(--spacing-xs);
  }

  .meta-name {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    font-weight: 500;
  }

  .meta-details {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  /* Tools tab */
  .tools-search {
    margin-bottom: var(--spacing-sm);
  }

  .tools-list {
    display: flex;
    flex-direction: column;
  }

  .tool-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) 0;
    border-bottom: 1px solid var(--border-subtle);
    min-height: var(--touch-target-min);
  }

  .tool-row:last-child {
    border-bottom: none;
  }

  .tool-row.tool-disabled {
    opacity: 0.5;
  }

  .tool-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .tool-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  /* Credential-axis nudge badge, matched to the mobile tool-panel scale.
     Warning tone for "needs_setup", neutral/muted for "pending". Only these
     two states render (see the authBadge snippet). */
  .auth-badge {
    display: inline-block;
    font-size: 9px;
    font-weight: 700;
    padding: 0 5px;
    margin-left: 4px;
    border-radius: var(--radius-sm);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    vertical-align: middle;
  }
  .auth-badge.needs-setup {
    background: rgba(var(--warning-rgb), 0.12);
    color: var(--warning);
    border: 1px solid rgba(var(--warning-rgb), 0.4);
  }
  .auth-badge.pending {
    background: var(--bg-elevated-2);
    color: var(--text-secondary);
    border: 1px solid var(--border-subtle);
  }

  .tool-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Skills tab */
  .skills-list {
    display: flex;
    flex-direction: column;
  }

  .skill-row {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) 0;
    border-bottom: 1px solid var(--border-subtle);
    min-height: 72px;
  }

  .skill-row:last-child {
    border-bottom: none;
  }

  .skill-row.skill-disabled {
    opacity: 0.62;
  }

  .skill-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .skill-head {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
  }

  .skill-name {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }

  .skill-scope,
  .skill-chip {
    display: inline-flex;
    align-items: center;
    min-height: 20px;
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    color: var(--text-muted);
    font-size: 10px;
    font-weight: 600;
  }

  .skill-chip {
    color: var(--accent-primary);
  }

  .skill-desc,
  .skill-tools {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  .skill-tools {
    color: var(--text-secondary);
  }

  .skill-actions {
    display: flex;
    flex-direction: column;
    gap: 6px;
    flex-shrink: 0;
  }

  .skill-btn {
    min-width: 76px;
    min-height: var(--touch-target-min);
    padding: 0 var(--spacing-xs);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: 600;
  }

  .skill-btn-active {
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
    color: var(--accent-primary);
  }

  .skill-btn-danger {
    border-color: var(--error);
    background: color-mix(in srgb, var(--error) 12%, transparent);
    color: var(--error);
  }

  /* Toggle switch */
  .toggle-btn {
    flex-shrink: 0;
    padding: 0;
    background: none;
    border: none;
  }

  .toggle-track {
    display: block;
    width: 40px;
    height: 22px;
    border-radius: 11px;
    background: var(--accent-primary);
    position: relative;
    transition: background var(--transition-normal);
  }

  .toggle-btn.off .toggle-track {
    background: var(--text-muted);
  }

  /* Knob travels via transform (compositor-only) at the shared toggle speed
     (150ms, matching desktop's ToggleSwitch), not `left` at 250ms. */
  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: white;
    transform: translateX(18px);
    transition: transform var(--transition-fast);
  }

  .toggle-btn.off .toggle-thumb {
    transform: translateX(0);
  }

  /* Error bar */
  .error-bar {
    padding: var(--spacing-sm) var(--spacing-md);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
    flex-shrink: 0;
  }

  /* Footer */
  .settings-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
    padding-bottom: calc(var(--spacing-sm) + var(--safe-area-bottom));
    gap: var(--spacing-sm);
  }

  .footer-right {
    display: flex;
    gap: var(--spacing-sm);
  }

  .footer-btn {
    padding: var(--spacing-xs) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-md);
    min-height: var(--touch-target-min);
  }

  .footer-btn:disabled {
    opacity: 0.4;
  }

  .footer-btn.reset {
    color: var(--text-muted);
  }

  .footer-btn.reset:active {
    color: var(--error);
  }

  .footer-btn.secondary {
    color: var(--text-secondary);
  }

  .footer-btn.secondary:active {
    background: var(--bg-hover);
  }

  .footer-btn.primary {
    color: var(--text-on-accent);
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .footer-btn.primary:active {
    filter: brightness(0.9);
  }

  /* Range inputs (for model tab if needed) */
  input[type="range"] {
    width: 100%;
    height: 4px;
    -webkit-appearance: none;
    appearance: none;
    background: var(--border-subtle);
    border-radius: 2px;
  }

  input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: var(--accent-primary);
  }

  input[type="number"] {
    -moz-appearance: textfield;
  }

  /* MCP Servers in tools tab */
  .mcp-server-group {
    border: 1px solid var(--border-subtle);
    border-radius: 6px;
    margin-bottom: 0.3rem;
    overflow: hidden;
  }

  .mcp-server-header-row {
    display: flex;
    align-items: center;
    background: var(--bg-elevated);
  }

  .mcp-server-header {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.5rem;
    background: none;
    border: none;
    color: var(--text-primary);
    font-size: 0.85rem;
    min-height: var(--touch-target-min);
  }

  .mcp-server-name {
    flex: 1;
    text-align: left;
    font-weight: 500;
  }

  .mcp-tool-count {
    font-size: 0.75rem;
    color: var(--text-secondary, var(--text-muted));
  }

  .mcp-bulk-btn {
    font-size: 0.75rem;
    color: var(--accent-primary);
    background: none;
    border: none;
    padding: 0.3rem 0.5rem;
    min-height: var(--touch-target-min);
  }

  .mcp-tool-list {
    border-top: 1px solid var(--border-subtle);
  }

  .mcp-add-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    padding: 0.5rem;
    margin-top: 0.4rem;
    border: 1px dashed var(--border-default);
    border-radius: 6px;
    background: none;
    color: var(--text-secondary);
    font-size: 0.85rem;
    width: 100%;
    min-height: var(--touch-target-min);
  }

  /* Chat App tab */
  .chatapp-cta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
  }

  .chatapp-cta-buttons {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .action-btn {
    padding: 12px 16px;
    min-height: var(--touch-target-min);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    text-align: center;
  }

  .action-btn:active {
    background: var(--bg-hover);
  }

  .action-btn.danger {
    color: var(--error);
    border-color: var(--error);
  }

  .binding-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .binding-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .binding-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .binding-provider {
    font-weight: 600;
    font-size: var(--font-size-sm);
    text-transform: capitalize;
  }

  .binding-chat {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .binding-type,
  .binding-when {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  /* Hooks tab — per-thread enablement */
  .hook-seg {
    display: inline-flex;
    gap: 2px;
    padding: 2px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    margin-top: var(--spacing-xs);
  }

  .hseg {
    padding: 5px 12px;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
    white-space: nowrap;
  }

  .hook-seg.small .hseg { padding: 4px 9px; }

  .hseg.active {
    background: var(--accent-primary);
    color: var(--text-on-accent, white);
  }

  .hook-ovr-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    transition: opacity var(--transition-fast);
  }

  .hook-ovr-list.dimmed { opacity: 0.5; }

  .hook-cat {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
  }

  .hook-cat-head {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    margin-bottom: 2px;
  }

  .hook-ovr-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) 0;
    border-top: 1px solid var(--glass-border);
  }

  .hook-cat .hook-ovr-row:first-of-type { border-top: none; }

  .hook-ovr-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .hook-ovr-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .hook-ovr-sub {
    font-size: 10px;
    color: var(--text-muted);
  }
</style>
