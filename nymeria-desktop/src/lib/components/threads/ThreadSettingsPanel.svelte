<script lang="ts">
  import { onMount } from 'svelte';
  import type { Thread, ThreadConfig, ThreadConfigUpdateRequest, ThreadPlatform, ProviderRoute } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { api } from '$lib/services/api.svelte';
  import { trapFocus } from '$lib/actions/focus';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { ToolCountWarning } from '$lib/components/tools';
  import { skillsStore } from '$lib/stores/skills.svelte';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import { computeEffectiveToolCounts, isMcpToolName, liveTemporaryToolNames } from '$lib/utils/toolCounts';
  import {
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    fromThreadDisplayProvider,
    supportsOpenAiApiMode,
    toThreadDisplayProvider,
    type ThreadDisplayProvider,
  } from '$lib/utils/providerMapping';
  import { detectThreadPlatform, isNativeDisplayPlatform } from '$lib/utils/platform';
  import BehaviorConfigTab from './BehaviorConfigTab.svelte';
  import ModelConfigTab from './ModelConfigTab.svelte';
  import ToolsConfigTab from './ToolsConfigTab.svelte';
  import SkillsConfigTab from './SkillsConfigTab.svelte';
  import MemoryConfigTab from './MemoryConfigTab.svelte';
  import DreamingConfigTab from './DreamingConfigTab.svelte';
  import AgentConfigTab from './AgentConfigTab.svelte';
  import ConnectionsConfigTab from './ConnectionsConfigTab.svelte';

  type ThreadSettingsTab =
    | 'behavior'
    | 'memory'
    | 'model-provider'
    | 'model-generation'
    | 'model-context'
    | 'tools-native'
    | 'tools-mcp'
    | 'skills'
    | 'dreaming'
    | 'agent'
    | 'connections';
  type TelegramAutonomousDelivery = ThreadConfig['telegramAutonomousDelivery'];
  type InAppNotificationLevel = ThreadConfig['inAppNotificationLevel'];

  // Vertical sidebar nav, grouped in the global Settings panel's idiom. The old
  // in-pane sub-tabs (Model: Provider/Generation/Context; Tools: Native/MCP) are
  // promoted to first-class nav items here.
  const NAV_GROUPS: { label: string; items: { id: ThreadSettingsTab; label: string; icon: string }[] }[] = [
    {
      label: 'Prompt',
      items: [
        { id: 'behavior', label: 'Behavior', icon: 'fileText' },
        { id: 'memory', label: 'Memory', icon: 'pin' },
      ],
    },
    {
      label: 'Model',
      items: [
        { id: 'model-provider', label: 'Provider', icon: 'server' },
        { id: 'model-generation', label: 'Generation', icon: 'settings' },
        { id: 'model-context', label: 'Context', icon: 'sort' },
      ],
    },
    {
      label: 'Capabilities',
      items: [
        { id: 'tools-native', label: 'Native Tools', icon: 'tool' },
        { id: 'tools-mcp', label: 'MCP Tools', icon: 'server' },
        { id: 'skills', label: 'Skills', icon: 'bolt' },
      ],
    },
    {
      label: 'Autonomous',
      items: [
        { id: 'dreaming', label: 'Dreaming', icon: 'clock' },
        { id: 'agent', label: 'Agent', icon: 'users' },
        { id: 'connections', label: 'Connections', icon: 'bell' },
      ],
    },
  ];

  // Map legacy / external initialTab values (incl. the previous eight-tab ids)
  // onto the new sidebar IA so existing callers (e.g. ThreadList's "configure as
  // agent") keep deep-linking sanely.
  function normalizeTab(tab: string | undefined): ThreadSettingsTab {
    switch (tab) {
      case 'model':
      case 'model-provider': return 'model-provider';
      case 'model-generation': return 'model-generation';
      case 'model-context': return 'model-context';
      case 'tools':
      case 'tools-native': return 'tools-native';
      case 'mcp':
      case 'tools-mcp': return 'tools-mcp';
      case 'skills': return 'skills';
      case 'memory':
      case 'notepad': return 'memory';
      case 'dreaming':
      case 'dream': return 'dreaming';
      case 'agent': return 'agent';
      case 'connections':
      case 'chatapp': return 'connections';
      case 'behavior':
      case 'instructions':
      case 'system-prompt':
      default: return 'behavior';
    }
  }

  interface Props {
    thread: Thread;
    threadConfig: ThreadConfig | null;
    initialTab?: string;
    onClose: () => void;
    onSaved: (config: ThreadConfig) => void;
  }

  let { thread, threadConfig, initialTab = 'behavior', onClose, onSaved }: Props = $props();

  function platformAfterCallableChange(target: Thread, callable: boolean): ThreadPlatform {
    const detected = detectThreadPlatform(target.id);
    if (isNativeDisplayPlatform(target.platform)) return target.platform;
    if (isNativeDisplayPlatform(detected)) return detected;
    if (callable) return 'callable';
    return target.platform === 'callable' ? 'desktop' : (target.platform ?? detected);
  }

  // Active tab
  // svelte-ignore state_referenced_locally — intentional: seed from the prop, then the effect keeps it synced
  let activeTab = $state<ThreadSettingsTab>(normalizeTab(initialTab));

  $effect(() => {
    activeTab = normalizeTab(initialTab);
  });

  let chatAppBindings = $derived(chatAppBindingsStore.getBindings(thread.id));

  // Per-thread skill overrides
  function getInitialThreadEnabledSkills(): Set<string> {
    return new Set(threadConfig?.enabledSkills ?? []);
  }

  function getInitialThreadDisabledSkills(): Set<string> {
    return new Set(threadConfig?.disabledSkills ?? []);
  }

  function skillNamesKey(names: Iterable<string> | null | undefined): string {
    return Array.from(names ?? []).sort().join('\x1f');
  }

  function threadSkillStateKey(
    enabled: Iterable<string> | null | undefined,
    disabled: Iterable<string> | null | undefined,
  ): string {
    return `${skillNamesKey(enabled)}\x1e${skillNamesKey(disabled)}`;
  }

  function threadSkillConfigKey(config: ThreadConfig | null): string {
    return threadSkillStateKey(config?.enabledSkills ?? [], config?.disabledSkills ?? []);
  }

  let threadEnabledSkills = $state<Set<string>>(getInitialThreadEnabledSkills());
  let threadDisabledSkills = $state<Set<string>>(getInitialThreadDisabledSkills());
  let appliedThreadSkillThreadId = $state('');
  let appliedThreadSkillConfigKey = $state('');

  $effect(() => {
    const nextKey = threadSkillConfigKey(threadConfig);
    const localKey = threadSkillStateKey(threadEnabledSkills, threadDisabledSkills);

    if (thread.id !== appliedThreadSkillThreadId) {
      threadEnabledSkills = getInitialThreadEnabledSkills();
      threadDisabledSkills = getInitialThreadDisabledSkills();
      appliedThreadSkillThreadId = thread.id;
      appliedThreadSkillConfigKey = nextKey;
      return;
    }

    if (nextKey === appliedThreadSkillConfigKey) return;

    if (localKey === appliedThreadSkillConfigKey || localKey === nextKey) {
      threadEnabledSkills = getInitialThreadEnabledSkills();
      threadDisabledSkills = getInitialThreadDisabledSkills();
      appliedThreadSkillConfigKey = nextKey;
    }
  });

  // Form state — initialized from threadConfig
  function getInitialInstructions(): string {
    return threadConfig?.instructions ?? '';
  }

  let instructions = $state(getInitialInstructions());

  // Per-thread notepad (the agent's persistent thread memory). Loaded lazily on
  // mount via a dedicated endpoint since it is a standalone markdown file, not
  // part of ThreadConfig.
  let notepad = $state('');
  let origNotepad = $state('');
  let notepadLoaded = $state(false);
  let notepadCharLimit = $state(0);

  // Derive initial tool state: if no per-thread config exists and global defaults
  // are customized, compute disabled/enabled from the default tool set so the UI
  // reflects what the agent will actually receive.
  function computeInitialToolState(): { disabled: Set<string>; enabled: Set<string> } {
    if (threadConfig?.hasCustomizations) {
      return {
        disabled: new Set(threadConfig.disabledTools ?? []),
        enabled: new Set(threadConfig.enabledTools ?? []),
      };
    }
    return { disabled: new Set(), enabled: new Set() };
  }

  const initialToolState = computeInitialToolState();
  let disabledTools = $state<Set<string>>(initialToolState.disabled);
  let enabledTools = $state<Set<string>>(initialToolState.enabled);

  function getInitialThreadDisplayProvider(): ThreadDisplayProvider {
    return toThreadDisplayProvider(
      threadConfig?.llmConfig?.provider ?? '',
      threadConfig?.llmConfig?.base_url
    );
  }

  function getInitialLlmProvider(): string {
    return threadConfig?.llmConfig?.provider ?? '';
  }

  function getInitialLlmModel(): string {
    return threadConfig?.llmConfig?.model ?? '';
  }

  function getInitialLlmBaseUrl(): string {
    return threadConfig?.llmConfig?.base_url ?? '';
  }

  function getInitialLlmApiKey(): string {
    return threadConfig?.llmConfig?.api_key ?? '';
  }

  function getInitialLlmTemperature(): string {
    return threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature)
      : '';
  }

  function getInitialLlmMaxTokens(): string {
    return threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens)
      : '';
  }

  function getInitialLlmContextLength(): string {
    return threadConfig?.llmConfig?.context_length != null
      ? String(threadConfig.llmConfig.context_length)
      : '';
  }

  function getInitialLlmOllamaNumCtx(): string {
    return threadConfig?.llmConfig?.ollama_num_ctx != null
      ? String(threadConfig.llmConfig.ollama_num_ctx)
      : '';
  }

  function getInitialLlmExtendedThinking(): 'default' | 'true' | 'false' {
    return threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) as 'true' | 'false'
      : 'default';
  }

  function getInitialLlmReasoningEffort(): string {
    return threadConfig?.llmConfig?.reasoning_effort ?? '';
  }

  function getInitialLlmUseModelDefaults(): 'default' | 'true' | 'false' {
    return threadConfig?.llmConfig?.use_model_defaults != null
      ? String(threadConfig.llmConfig.use_model_defaults) as 'true' | 'false'
      : 'default';
  }

  function getInitialLlmOpenAiApiMode(): 'default' | 'chat_completions' | 'responses' {
    return threadConfig?.llmConfig?.openai_api_mode ?? 'default';
  }

  function getInitialLlmProviderRoute(): 'default' | ProviderRoute {
    return threadConfig?.llmConfig?.provider_route ?? 'default';
  }

  function getInitialCompactThresholdMode(): 'default' | 'percentage' | 'tokens' {
    return threadConfig?.llmConfig?.compact_threshold_mode ?? 'default';
  }

  function getInitialCompactThreshold(): string {
    return threadConfig?.llmConfig?.compact_threshold != null
      ? String(threadConfig.llmConfig.compact_threshold)
      : '';
  }

  function getInitialCompactThresholdTokens(): string {
    return threadConfig?.llmConfig?.compact_threshold_tokens != null
      ? String(threadConfig.llmConfig.compact_threshold_tokens)
      : '';
  }

  // LLM form state
  let threadDisplayProvider = $state<ThreadDisplayProvider>(getInitialThreadDisplayProvider());
  let llmProvider = $state(getInitialLlmProvider());
  let llmModel = $state(getInitialLlmModel());
  let llmBaseUrl = $state(getInitialLlmBaseUrl());
  let llmApiKey = $state(getInitialLlmApiKey());
  let llmTemperature = $state<string>(getInitialLlmTemperature());
  let llmMaxTokens = $state<string>(getInitialLlmMaxTokens());
  let llmContextLength = $state<string>(getInitialLlmContextLength());
  let llmOllamaNumCtx = $state<string>(getInitialLlmOllamaNumCtx());
  let llmExtendedThinking = $state<'default' | 'true' | 'false'>(getInitialLlmExtendedThinking());
  let llmReasoningEffort = $state(getInitialLlmReasoningEffort());
  let llmUseModelDefaults = $state<'default' | 'true' | 'false'>(getInitialLlmUseModelDefaults());
  let llmProviderRoute = $state<'default' | ProviderRoute>(getInitialLlmProviderRoute());
  let llmOpenAiApiMode = $state<'default' | 'chat_completions' | 'responses'>(getInitialLlmOpenAiApiMode());
  let compactThresholdMode = $state<'default' | 'percentage' | 'tokens'>(getInitialCompactThresholdMode());
  let compactThresholdPct = $state<string>(getInitialCompactThreshold());
  let compactThresholdTokens = $state<string>(getInitialCompactThresholdTokens());

  function getEffectiveProvider(): string {
    return llmProvider || serverSettingsStore.provider || '';
  }

  // System prompt & agent fields
  function getInitialSystemPrompt(): string {
    return threadConfig?.systemPrompt ?? '';
  }

  function getInitialCallable(): boolean {
    return threadConfig?.callable ?? false;
  }

  function getInitialCallableName(): string {
    return threadConfig?.callableName ?? '';
  }

  function getInitialCallableDescription(): string {
    return threadConfig?.callableDescription ?? '';
  }

  let systemPrompt = $state(getInitialSystemPrompt());
  let isCallable = $state(getInitialCallable());
  let callableName = $state(getInitialCallableName());
  let callableDescription = $state(getInitialCallableDescription());

  // Visibility / advanced
  function getInitialInjectTodosInPrompt(): boolean {
    return threadConfig?.injectTodosInPrompt ?? false;
  }

  function getInitialShowAutonomousPrompts(): boolean {
    return threadConfig?.showAutonomousPrompts ?? false;
  }

  function getInitialShowPromptMetadata(): boolean {
    return threadConfig?.showPromptMetadata ?? false;
  }

  function getInitialTelegramAutonomousDelivery(): TelegramAutonomousDelivery {
    return threadConfig?.telegramAutonomousDelivery ?? 'full';
  }

  function getInitialInAppNotificationLevel(): InAppNotificationLevel {
    return threadConfig?.inAppNotificationLevel ?? 'notify_only';
  }

  function getInitialNotificationProfile(): string | null {
    return threadConfig?.notificationProfile ?? null;
  }

  function getInitialMemoryCharLimit(): string {
    return threadConfig?.memoryCharLimit != null ? String(threadConfig.memoryCharLimit) : '';
  }

  function getInitialDreamEnabled(): boolean {
    return threadConfig?.dreaming?.enabled ?? false;
  }

  // Blank when the thread has no per-thread override: the field then inherits
  // the global dreaming default (Settings, Dreaming), resolved at dream time.
  function getInitialDreamMinIntervalHours(): string {
    return threadConfig?.dreaming?.minIntervalHours != null ? String(threadConfig.dreaming.minIntervalHours) : '';
  }

  function getInitialDreamMinIdleMinutes(): string {
    return threadConfig?.dreaming?.minIdleMinutes != null ? String(threadConfig.dreaming.minIdleMinutes) : '';
  }

  function getInitialDreamMinTurnsSinceLast(): string {
    return threadConfig?.dreaming?.minTurnsSinceLast != null ? String(threadConfig.dreaming.minTurnsSinceLast) : '';
  }

  function getInitialDreamModel(): string {
    return threadConfig?.dreaming?.model ?? '';
  }
  function getInitialDreamSystemPrompt(): string {
    return threadConfig?.dreaming?.systemPrompt ?? '';
  }
  function getInitialDreamKickoffPrompt(): string {
    return threadConfig?.dreaming?.kickoffPrompt ?? '';
  }

  let injectTodosInPrompt = $state(getInitialInjectTodosInPrompt());
  let showAutonomousPrompts = $state(getInitialShowAutonomousPrompts());
  let showPromptMetadata = $state(getInitialShowPromptMetadata());
  let telegramAutonomousDelivery = $state<TelegramAutonomousDelivery>(getInitialTelegramAutonomousDelivery());
  let inAppNotificationLevel = $state<InAppNotificationLevel>(getInitialInAppNotificationLevel());
  let notificationProfile = $state<string | null>(getInitialNotificationProfile());
  let memoryCharLimit = $state<string | number>(getInitialMemoryCharLimit());
  let dreamEnabled = $state(getInitialDreamEnabled());
  let dreamMinIntervalHours = $state<string>(getInitialDreamMinIntervalHours());
  let dreamMinIdleMinutes = $state<string>(getInitialDreamMinIdleMinutes());
  let dreamMinTurnsSinceLast = $state<string>(getInitialDreamMinTurnsSinceLast());
  let dreamModel = $state(getInitialDreamModel());
  let dreamSystemPrompt = $state(getInitialDreamSystemPrompt());
  let dreamKickoffPrompt = $state(getInitialDreamKickoffPrompt());
  let dreamRunning = $state(false);
  let dreamStatus = $state('');

  let saving = $state(false);
  let error = $state('');
  let showToolWarning = $state(false);

  // Tools tab transient UI state lives here (not in ToolsConfigTab) so the
  // search query and group expand/collapse survive switching tabs. Native and
  // MCP keep separate state so a query in one sub-tab does not carry over and
  // filter the other (different vocabulary) down to nothing.
  let toolSearchNative = $state('');
  let toolSearchMcp = $state('');
  let toolsExpandedNative = $state<Set<string>>(new Set());
  let toolsExpandedMcp = $state<Set<string>>(new Set());
  let toolsCollapsedNative = $state<Set<string>>(new Set());
  let toolsCollapsedMcp = $state<Set<string>>(new Set());
  // Enabled/Available pool open state, hoisted so collapsing one pool survives
  // switching between the Native and MCP sub-tabs (Enabled open, Available
  // closed by default, mirroring the global Tools panel).
  let toolsEnabledOpenNative = $state(true);
  let toolsAvailableOpenNative = $state(false);
  let toolsEnabledOpenMcp = $state(true);
  let toolsAvailableOpenMcp = $state(false);

  // Effective active tool count for this thread (default minus disabled, plus
  // optional enabled + live TTL'd) — drives the >25 tool warning shown on save.
  const effectiveToolCount = $derived(
    computeEffectiveToolCounts({
      defaultToolNames: defaultToolsStore.defaultToolNames,
      enabledTools: [...enabledTools],
      disabledTools: [...disabledTools],
      temporaryTools: liveTemporaryToolNames(threadConfig?.temporaryTools),
    }).totalActiveCount
  );

  // Per-nav-item "customized" indicators — a quiet dot on the sidebar item that
  // carries an override (accent stays reserved for the active item). Model and
  // Tools are split by section/source so the dot lands on the exact sub-item.
  const connectionsCount = $derived(chatAppBindings.length);
  const behaviorCustomized = $derived(
    Boolean(instructions.trim() || systemPrompt.trim() || injectTodosInPrompt || showAutonomousPrompts || showPromptMetadata)
  );
  const modelProviderCustomized = $derived(
    Boolean(
      threadDisplayProvider || llmModel || llmBaseUrl || llmApiKey ||
      llmProviderRoute !== 'default' || llmOpenAiApiMode !== 'default'
    )
  );
  const modelGenerationCustomized = $derived(
    Boolean(
      llmTemperature || llmMaxTokens || llmExtendedThinking !== 'default' ||
      llmReasoningEffort || llmUseModelDefaults !== 'default'
    )
  );
  const modelContextCustomized = $derived(
    Boolean(
      llmContextLength || llmOllamaNumCtx || compactThresholdMode !== 'default' ||
      compactThresholdPct || compactThresholdTokens
    )
  );
  const memoryCustomized = $derived(Boolean(String(memoryCharLimit ?? '').trim()));
  const skillsCustomized = $derived(threadEnabledSkills.size + threadDisabledSkills.size > 0);
  const toolOverrides = $derived([...disabledTools, ...enabledTools]);
  const nativeToolsCustomized = $derived(toolOverrides.some((n) => !isMcpToolName(n)));
  const mcpToolsCustomized = $derived(toolOverrides.some((n) => isMcpToolName(n)));
  const dreamingActive = $derived(dreamEnabled);
  const agentActive = $derived(isCallable);

  const navCustomized: Record<ThreadSettingsTab, boolean> = $derived({
    behavior: behaviorCustomized,
    memory: memoryCustomized,
    'model-provider': modelProviderCustomized,
    'model-generation': modelGenerationCustomized,
    'model-context': modelContextCustomized,
    'tools-native': nativeToolsCustomized,
    'tools-mcp': mcpToolsCustomized,
    skills: skillsCustomized,
    dreaming: dreamingActive,
    agent: agentActive,
    connections: connectionsCount > 0,
  });

  // Which content group is mounted. Switching sub-items within Model or Tools
  // keeps the same component instance (no remount, no provider/model refetch).
  const contentKey = $derived(
    activeTab.startsWith('model-') ? 'model' : activeTab.startsWith('tools-') ? 'tools' : activeTab
  );
  const modelSection = $derived(
    activeTab === 'model-generation' ? 'generation' : activeTab === 'model-context' ? 'context' : 'provider'
  );
  const toolSection = $derived(activeTab === 'tools-mcp' ? 'mcp' : 'native');

  const effectiveModelLabel = $derived(llmModel || serverSettingsStore.model || 'Global default');
  const modelInherited = $derived(!llmModel);

  // Ensure tools and model metadata are loaded for the child tabs.
  $effect(() => {
    if (!unifiedToolsStore.loaded && !unifiedToolsStore.loading) {
      unifiedToolsStore.loadTools();
    }
    if (!modelsStore.loaded && !modelsStore.loading) {
      modelsStore.loadModels();
    }
    if (!defaultToolsStore.loaded && !defaultToolsStore.loading) {
      defaultToolsStore.load();
    }
    if (!skillsStore.installedLoaded && !skillsStore.installedLoading) {
      skillsStore.loadInstalled();
    }
    if (!skillsStore.enabledGlobalLoaded && !skillsStore.enabledGlobalLoading) {
      skillsStore.loadGlobal();
    }
  });

  // Force-refresh MCP servers + default tools on panel mount so changes made
  // in the global Settings → Tools / MCP panel are reflected here without a
  // full app reload.
  onMount(() => {
    mcpServersStore.refresh();
    defaultToolsStore.resetLoaded();
    void defaultToolsStore.load();
    void skillsStore.refreshInstalled();
    void skillsStore.refreshGlobal();
    // Load chat-app bindings up front so the Connections tab badge is accurate
    // before that tab is opened.
    void chatAppBindingsStore.loadBindings(thread.id).catch(() => {});
    void threadConfigStore.loadConfig(thread.id).catch((err) => {
      console.warn('[ThreadSettingsPanel] Failed to refresh thread config:', err);
    });
    void api.getThreadNotepad(thread.id).then((np) => {
      notepad = np.content;
      origNotepad = np.content;
      notepadCharLimit = np.charLimit;
      notepadLoaded = true;
    }).catch((err) => {
      console.warn('[ThreadSettingsPanel] Failed to load notepad:', err);
      notepadLoaded = true;
    });
  });

  function boundedInt(value: string | number, fallback: number, min: number, max: number): number {
    const parsed = parseInt(String(value ?? '').trim(), 10);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
  }

  // A blank dreaming threshold means "inherit the global default" -> null.
  function optionalBoundedInt(value: string | number, min: number, max: number): number | null {
    const raw = String(value ?? '').trim();
    if (raw === '') return null;
    const parsed = parseInt(raw, 10);
    if (!Number.isFinite(parsed)) return null;
    return Math.min(max, Math.max(min, parsed));
  }

  function dreamConfigNeedsSaving(): boolean {
    return Boolean(
      threadConfig?.dreaming ||
      dreamEnabled ||
      dreamModel.trim() ||
      dreamSystemPrompt.trim() ||
      dreamKickoffPrompt.trim() ||
      dreamMinIntervalHours.trim() ||
      dreamMinIdleMinutes.trim() ||
      dreamMinTurnsSinceLast.trim()
    );
  }

  function hasChanges(): boolean {
    const origInstructions = threadConfig?.instructions ?? '';
    const origDisabled = new Set(threadConfig?.disabledTools ?? []);
    const origProvider = threadConfig?.llmConfig?.provider ?? '';
    const origModel = threadConfig?.llmConfig?.model ?? '';
    const origBaseUrl = threadConfig?.llmConfig?.base_url ?? '';
    const origApiKey = threadConfig?.llmConfig?.api_key ?? '';
    const origTemp = threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature) : '';
    const origMaxTokens = threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens) : '';
    const origContextLength = threadConfig?.llmConfig?.context_length != null
      ? String(threadConfig.llmConfig.context_length) : '';
    const origOllamaNumCtx = threadConfig?.llmConfig?.ollama_num_ctx != null
      ? String(threadConfig.llmConfig.ollama_num_ctx) : '';
    const origExtThinking = threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) : 'default';
    const origReasoning = threadConfig?.llmConfig?.reasoning_effort ?? '';
    const origUseModelDefaults = threadConfig?.llmConfig?.use_model_defaults != null
      ? String(threadConfig.llmConfig.use_model_defaults) : 'default';
    const origProviderRoute = threadConfig?.llmConfig?.provider_route ?? 'default';
    const origOpenAiApiMode = threadConfig?.llmConfig?.openai_api_mode ?? 'default';
    const origCompactMode = threadConfig?.llmConfig?.compact_threshold_mode ?? 'default';
    const origCompactPct = threadConfig?.llmConfig?.compact_threshold != null
      ? String(threadConfig.llmConfig.compact_threshold) : '';
    const origCompactTokens = threadConfig?.llmConfig?.compact_threshold_tokens != null
      ? String(threadConfig.llmConfig.compact_threshold_tokens) : '';

    const origEnabled = new Set(threadConfig?.enabledTools ?? []);
    const origSystemPrompt = threadConfig?.systemPrompt ?? '';
    const origCallable = threadConfig?.callable ?? false;
    const origCallableName = threadConfig?.callableName ?? '';
    const origCallableDescription = threadConfig?.callableDescription ?? '';

    if (instructions !== origInstructions) return true;
    if (disabledTools.size !== origDisabled.size) return true;
    for (const t of disabledTools) {
      if (!origDisabled.has(t)) return true;
    }
    if (enabledTools.size !== origEnabled.size) return true;
    for (const t of enabledTools) {
      if (!origEnabled.has(t)) return true;
    }
    const origEnabledSkills = new Set(threadConfig?.enabledSkills ?? []);
    const origDisabledSkills = new Set(threadConfig?.disabledSkills ?? []);
    if (threadEnabledSkills.size !== origEnabledSkills.size) return true;
    for (const s of threadEnabledSkills) if (!origEnabledSkills.has(s)) return true;
    if (threadDisabledSkills.size !== origDisabledSkills.size) return true;
    for (const s of threadDisabledSkills) if (!origDisabledSkills.has(s)) return true;
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
    if (llmUseModelDefaults !== origUseModelDefaults) return true;
    if (llmProviderRoute !== origProviderRoute) return true;
    if (llmOpenAiApiMode !== origOpenAiApiMode) return true;
    if (compactThresholdMode !== origCompactMode) return true;
    if (compactThresholdPct !== origCompactPct) return true;
    if (compactThresholdTokens !== origCompactTokens) return true;
    if (systemPrompt !== origSystemPrompt) return true;
    if (notepad !== origNotepad) return true;
    if (isCallable !== origCallable) return true;
    if (callableName !== origCallableName) return true;
    if (callableDescription !== origCallableDescription) return true;

    const origInjectTodos = threadConfig?.injectTodosInPrompt ?? false;
    if (injectTodosInPrompt !== origInjectTodos) return true;

    const origShowAutonomous = threadConfig?.showAutonomousPrompts ?? false;
    if (showAutonomousPrompts !== origShowAutonomous) return true;

    const origShowPromptMeta = threadConfig?.showPromptMetadata ?? false;
    if (showPromptMetadata !== origShowPromptMeta) return true;

    const origTelegramDelivery = threadConfig?.telegramAutonomousDelivery ?? 'full';
    if (telegramAutonomousDelivery !== origTelegramDelivery) return true;

    const origNotificationLevel = threadConfig?.inAppNotificationLevel ?? 'notify_only';
    if (inAppNotificationLevel !== origNotificationLevel) return true;
    const origNotificationProfile = threadConfig?.notificationProfile ?? null;
    if ((notificationProfile ?? null) !== origNotificationProfile) return true;

    const origMemoryCharLimit = threadConfig?.memoryCharLimit != null ? String(threadConfig.memoryCharLimit) : '';
    if (String(memoryCharLimit ?? '').trim() !== origMemoryCharLimit) return true;

    const origDream = threadConfig?.dreaming ?? null;
    const origDreamEnabled = origDream?.enabled ?? false;
    const origDreamMinIntervalHours = origDream?.minIntervalHours != null ? String(origDream.minIntervalHours) : '';
    const origDreamMinIdleMinutes = origDream?.minIdleMinutes != null ? String(origDream.minIdleMinutes) : '';
    const origDreamMinTurnsSinceLast = origDream?.minTurnsSinceLast != null ? String(origDream.minTurnsSinceLast) : '';
    const origDreamModel = origDream?.model ?? '';
    const origDreamSystemPrompt = origDream?.systemPrompt ?? '';
    const origDreamKickoffPrompt = origDream?.kickoffPrompt ?? '';
    if (dreamEnabled !== origDreamEnabled) return true;
    if (String(dreamMinIntervalHours ?? '').trim() !== origDreamMinIntervalHours) return true;
    if (String(dreamMinIdleMinutes ?? '').trim() !== origDreamMinIdleMinutes) return true;
    if (String(dreamMinTurnsSinceLast ?? '').trim() !== origDreamMinTurnsSinceLast) return true;
    if (dreamModel !== origDreamModel) return true;
    if (dreamSystemPrompt !== origDreamSystemPrompt) return true;
    if (dreamKickoffPrompt !== origDreamKickoffPrompt) return true;

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

      // Disabled tools are authoritative thread overrides and must be preserved
      // even if the tool is not currently in the global default set.
      const effectiveDisabled = [...disabledTools];
      if (effectiveDisabled.length > 0) {
        updates.disabled_tools = effectiveDisabled;
      } else {
        updates.clear_disabled_tools = true;
      }

      // Enabled optional tools
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
      const hasLlm = threadDisplayProvider || llmModel || llmTemperature || llmMaxTokens ||
        llmContextLength || llmOllamaNumCtx ||
        llmExtendedThinking !== 'default' || llmReasoningEffort ||
        llmUseModelDefaults !== 'default' || llmProviderRoute !== 'default' ||
        llmOpenAiApiMode !== 'default' || llmBaseUrl || llmApiKey ||
        compactThresholdMode !== 'default' || compactThresholdPct || compactThresholdTokens;

      if (hasLlm) {
        const llm: Record<string, unknown> = {};
        const mapped = fromThreadDisplayProvider(threadDisplayProvider);
        llm.provider = mapped.provider || null;
        const effectiveProviderForSave = mapped.provider || getEffectiveProvider();
        if (threadDisplayProvider === 'openai_custom') {
          llm.base_url = llmBaseUrl || DEFAULT_CUSTOM_OPENAI_BASE_URL;
        } else if (supportsOpenAiApiMode(effectiveProviderForSave)) {
          llm.base_url = llmBaseUrl || mapped.baseUrl;
        } else {
          llm.base_url = mapped.baseUrl;
        }
        llm.api_key = llmApiKey || null;
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
        llm.openai_api_mode = supportsOpenAiApiMode(getEffectiveProvider()) && llmOpenAiApiMode !== 'default'
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

      // Callable fields
      updates.callable = isCallable;
      if (isCallable) {
        updates.callable_name = callableName.trim() || null;
        updates.callable_description = callableDescription.trim() || null;
      }

      // Visibility / advanced
      updates.inject_todos_in_prompt = injectTodosInPrompt;
      updates.show_autonomous_prompts = showAutonomousPrompts;
      updates.show_prompt_metadata = showPromptMetadata;
      updates.telegram_autonomous_delivery = telegramAutonomousDelivery;
      updates.in_app_notification_level = inAppNotificationLevel;
      if (notificationProfile === null || notificationProfile === '') {
        updates.clear_notification_profile = true;
      } else {
        updates.notification_profile = notificationProfile;
      }
      const memoryLimitValue = String(memoryCharLimit ?? '').trim();
      if (memoryLimitValue) {
        updates.memory_char_limit = parseInt(memoryLimitValue, 10);
      } else {
        updates.clear_memory_char_limit = true;
      }
      if (dreamConfigNeedsSaving()) {
        updates.dreaming = {
          enabled: dreamEnabled,
          // Blank fields -> null: inherit the global dreaming defaults.
          min_interval_hours: optionalBoundedInt(dreamMinIntervalHours, 1, 168),
          min_idle_minutes: optionalBoundedInt(dreamMinIdleMinutes, 5, 10080),
          min_turns_since_last: optionalBoundedInt(dreamMinTurnsSinceLast, 1, 10000),
          model: dreamModel.trim() || null,
          system_prompt: dreamSystemPrompt.trim() || null,
          kickoff_prompt: dreamKickoffPrompt.trim() || null,
        };
      } else {
        updates.clear_dreaming = true;
      }

      const result = await threadConfigStore.updateConfig(thread.id, updates);

      // The notepad is a standalone store (not part of ThreadConfig), so save it
      // separately when it changed. A char-limit violation surfaces as an error.
      if (notepad !== origNotepad) {
        const np = await api.updateThreadNotepad(thread.id, notepad);
        notepad = np.content;
        origNotepad = np.content;
        notepadCharLimit = np.charLimit;
      }

      if (isCallable && callableName.trim()) {
        threadsStore.applyBackendTitle(thread.id, callableName.trim());
      }
      threadsStore.updateThread(thread.id, {
        callable: result.callable,
        platform: platformAfterCallableChange(thread, result.callable),
      });

      onSaved(result);

      const savedThreadId = thread.id;
      api.getThreadContextStats(savedThreadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === savedThreadId) {
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats.model);
        }
      });

      onClose();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  // Reset every form field to its "no override" baseline. The notepad is the
  // agent's persistent memory (not a config override), so it is intentionally
  // left untouched here.
  function resetFormState() {
    instructions = '';
    systemPrompt = '';
    disabledTools = new Set();
    enabledTools = new Set();
    threadEnabledSkills = new Set();
    threadDisabledSkills = new Set();
    threadDisplayProvider = toThreadDisplayProvider('', undefined);
    llmProvider = '';
    llmModel = '';
    llmBaseUrl = '';
    llmApiKey = '';
    llmTemperature = '';
    llmMaxTokens = '';
    llmContextLength = '';
    llmOllamaNumCtx = '';
    llmExtendedThinking = 'default';
    llmReasoningEffort = '';
    llmUseModelDefaults = 'default';
    llmProviderRoute = 'default';
    llmOpenAiApiMode = 'default';
    compactThresholdMode = 'default';
    compactThresholdPct = '';
    compactThresholdTokens = '';
    isCallable = false;
    callableName = '';
    callableDescription = '';
    injectTodosInPrompt = false;
    showAutonomousPrompts = false;
    showPromptMetadata = false;
    telegramAutonomousDelivery = 'full';
    inAppNotificationLevel = 'notify_only';
    notificationProfile = null;
    memoryCharLimit = '';
    dreamEnabled = false;
    dreamMinIntervalHours = '';
    dreamMinIdleMinutes = '';
    dreamMinTurnsSinceLast = '';
    dreamModel = '';
    dreamSystemPrompt = '';
    dreamKickoffPrompt = '';
    dreamStatus = '';
  }

  async function handleReset() {
    saving = true;
    error = '';
    try {
      await threadConfigStore.deleteConfig(thread.id);
      resetFormState();
      threadsStore.updateThread(thread.id, {
        callable: false,
        platform: platformAfterCallableChange(thread, false),
      });
      onSaved({
        threadId: thread.id,
        instructions: null,
        disabledTools: [],
        enabledTools: [],
        temporaryTools: {},
        enabledSkills: [],
        disabledSkills: [],
        llmConfig: null,
        systemPrompt: null,
        callable: false,
        callableName: null,
        callableDescription: null,
        injectTodosInPrompt: false,
        showAutonomousPrompts: false,
        showPromptMetadata: false,
        telegramAutonomousDelivery: 'full',
        inAppNotificationLevel: 'notify_only',
        notificationProfile: null,
        memoryCharLimit: null,
        createdAt: null,
        updatedAt: null,
        hasCustomizations: false,
      });

      const resetThreadId = thread.id;
      api.getThreadContextStats(resetThreadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === resetThreadId) {
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats.model);
        }
      });

      onClose();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to reset';
    } finally {
      saving = false;
    }
  }

  async function handleRunDream() {
    dreamRunning = true;
    dreamStatus = '';
    error = '';
    try {
      const result = await api.triggerThreadDream(thread.id, {
        model: dreamModel.trim() || null,
      });
      dreamStatus = `Started ${result.shadow_thread_id}`;
      await threadsStore.syncFromBackend();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to start dream';
    } finally {
      dreamRunning = false;
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="modal-backdrop">
  <button
    class="modal-backdrop-button"
    type="button"
    tabindex="-1"
    aria-label="Close thread settings"
    onclick={onClose}
  ></button>
  <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="thread-settings-title" tabindex="-1" use:trapFocus>
    <div class="modal-header">
      <div class="header-row">
        <div class="header-titles">
          <h2 id="thread-settings-title">Thread Settings</h2>
          <span class="modal-subtitle">{thread.title}</span>
        </div>
        <span class="header-model" title="Effective model for this thread">
          <span class="header-model-label">Model</span>
          <span class="header-model-value">{effectiveModelLabel}</span>
          {#if modelInherited}<span class="header-model-tag">global default</span>{/if}
        </span>
        <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
          <Icon name="x" size={18} />
        </button>
      </div>
      <p class="scope-line">Overrides apply to this thread only. Unset fields inherit your global defaults.</p>
    </div>

    <div class="modal-body">
      <aside class="settings-sidebar" aria-label="Thread settings sections">
        {#each NAV_GROUPS as group (group.label)}
          <div class="nav-group">
            <span class="nav-group-label">{group.label}</span>
            {#each group.items as item (item.id)}
              <button
                class="nav-item"
                class:active={activeTab === item.id}
                onclick={() => (activeTab = item.id)}
                type="button"
              >
                <Icon name={item.icon} size={14} />
                <span class="nav-item-label">{item.label}</span>
                {#if navCustomized[item.id]}<span class="nav-dot" aria-hidden="true"></span>{/if}
              </button>
            {/each}
          </div>
        {/each}
      </aside>

      <main class="settings-content">
        {#key contentKey}
          <div class="tab-fade">
            {#if activeTab === 'behavior'}
              <BehaviorConfigTab
                bind:instructions
                bind:systemPrompt
                bind:injectTodosInPrompt
                bind:showAutonomousPrompts
                bind:showPromptMetadata
              />
            {:else if activeTab === 'memory'}
              <MemoryConfigTab
                bind:notepad
                {notepadLoaded}
                {notepadCharLimit}
                bind:memoryCharLimit
                globalMemoryLimit={serverSettingsStore.memoryCharLimit}
              />
            {:else if activeTab === 'model-provider' || activeTab === 'model-generation' || activeTab === 'model-context'}
              <ModelConfigTab
                section={modelSection}
                bind:threadDisplayProvider
                bind:llmProvider
                bind:llmModel
                bind:llmBaseUrl
                bind:llmApiKey
                bind:llmTemperature
                bind:llmMaxTokens
                bind:llmContextLength
                bind:llmOllamaNumCtx
                bind:llmExtendedThinking
                bind:llmReasoningEffort
                bind:llmUseModelDefaults
                bind:llmProviderRoute
                bind:llmOpenAiApiMode
                bind:compactThresholdMode
                bind:compactThresholdPct
                bind:compactThresholdTokens
              />
            {:else if activeTab === 'tools-native' || activeTab === 'tools-mcp'}
              {#if toolSection === 'mcp'}
                <ToolsConfigTab
                  section="mcp"
                  threadId={thread.id}
                  bind:disabledTools
                  bind:enabledTools
                  temporaryTools={threadConfig?.temporaryTools}
                  bind:query={toolSearchMcp}
                  bind:expanded={toolsExpandedMcp}
                  bind:collapsed={toolsCollapsedMcp}
                  bind:enabledOpen={toolsEnabledOpenMcp}
                  bind:availableOpen={toolsAvailableOpenMcp}
                />
              {:else}
                <ToolsConfigTab
                  section="native"
                  threadId={thread.id}
                  bind:disabledTools
                  bind:enabledTools
                  temporaryTools={threadConfig?.temporaryTools}
                  bind:query={toolSearchNative}
                  bind:expanded={toolsExpandedNative}
                  bind:collapsed={toolsCollapsedNative}
                  bind:enabledOpen={toolsEnabledOpenNative}
                  bind:availableOpen={toolsAvailableOpenNative}
                />
              {/if}
            {:else if activeTab === 'skills'}
              <SkillsConfigTab
                bind:threadEnabledSkills
                bind:threadDisabledSkills
              />
            {:else if activeTab === 'dreaming'}
              <DreamingConfigTab
                bind:dreamEnabled
                bind:dreamMinIntervalHours
                bind:dreamMinIdleMinutes
                bind:dreamMinTurnsSinceLast
                bind:dreamModel
                bind:dreamSystemPrompt
                bind:dreamKickoffPrompt
                lastDreamAt={threadConfig?.dreaming?.lastDreamAt}
                {dreamRunning}
                {dreamStatus}
                {saving}
                hasUnsavedChanges={hasChanges()}
                onRunDream={handleRunDream}
              />
            {:else if activeTab === 'agent'}
              <AgentConfigTab
                bind:isCallable
                bind:callableName
                bind:callableDescription
              />
            {:else if activeTab === 'connections'}
              <ConnectionsConfigTab
                {thread}
                bind:telegramAutonomousDelivery
                bind:inAppNotificationLevel
                bind:notificationProfile
              />
            {/if}
          </div>
        {/key}
      </main>
    </div>

    {#if error}
      <div class="error-bar">{error}</div>
    {/if}

    <div class="modal-footer">
      <button class="btn btn-ghost btn-reset" onclick={handleReset} disabled={saving} type="button">
        Reset thread to defaults
      </button>
      <div class="footer-right">
        <button class="btn btn-ghost" onclick={onClose} disabled={saving} type="button">
          Cancel
        </button>
        <button
          class="btn btn-primary"
          onclick={checkToolCountAndSave}
          disabled={saving || !hasChanges()}
          type="button"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>
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

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-xl);
    box-sizing: border-box;
    z-index: 1000;
  }

  .modal-backdrop-button {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: transparent;
  }

  .modal-panel {
    position: relative;
    background: var(--bg-base);
    border-radius: var(--radius-lg);
    /* Grow with the window so maximising/fullscreen uses the space, but stay
       capped so forms don't stretch absurdly wide. */
    width: min(1080px, 94vw);
    height: min(840px, 90vh);
    display: flex;
    flex-direction: column;
    /* §7 — floating modal panel: shadow alone defines elevation; border
       would be redundant chrome. Tokenized to --shadow-xl. */
    box-shadow: var(--shadow-xl);
    box-sizing: border-box;
    overflow: hidden;
  }

  /* Header — plain and thread-scoped: title + thread name on the left, the
     effective model as quiet bold text on the right, then a one-line scope
     note. No accent fills. */
  .modal-header {
    flex-shrink: 0;
    padding: 14px var(--spacing-lg) 12px;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-default);
  }

  .header-row {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-md);
  }

  .header-titles {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-sm);
    min-width: 0;
    flex: 1;
  }

  .modal-header h2 {
    margin: 0;
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.005em;
    flex-shrink: 0;
  }

  .modal-subtitle {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .header-model {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    flex-shrink: 0;
    max-width: 340px;
    white-space: nowrap;
    overflow: hidden;
  }

  .header-model-label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .header-model-value {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .header-model-tag {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    flex-shrink: 0;
    transition: color var(--transition-fast), background var(--transition-fast);
  }

  .close-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .scope-line {
    margin: 8px 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  /* Two-column body: a global-style vertical nav sidebar on the left, the
     active section's content (the single scroll surface) on the right. */
  .modal-body {
    display: flex;
    flex: 1;
    min-height: 0;
  }

  /* Sidebar mirrors the global Settings panel's nav idiom (grouped nav-items)
     so the two menus share one navigation language; only the layout around it
     differs (this panel keeps its thread header + Save/Reset footer). */
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

  .nav-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }
  .nav-item:hover :global(svg) {
    color: var(--text-secondary);
  }

  .nav-item.active {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
  }
  .nav-item.active :global(svg) {
    color: var(--accent-primary);
  }

  .nav-item-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .nav-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--text-muted);
    flex-shrink: 0;
  }

  /* No scrollbar-width here: that switches WebView2/Chromium to the standard
     (white) scrollbar and ignores the app's global ::-webkit-scrollbar theme.
     Letting it inherit keeps the themed thin scrollbar. The tab bodies supply
     their own padding, so the content column adds none. */
  .settings-content {
    flex: 1;
    min-width: 0;
    overflow-y: auto;
  }

  .tab-fade {
    animation: threadTabFade 180ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  @keyframes threadTabFade {
    from { opacity: 0; transform: translateY(3px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Footer */
  .modal-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-top: 1px solid var(--border-default);
    background: var(--bg-elevated);
    flex-shrink: 0;
  }

  .footer-right {
    display: flex;
    gap: var(--spacing-sm);
  }

  .btn {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-ghost {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
  }

  .btn-ghost:hover:not(:disabled) {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .btn-reset:hover:not(:disabled) {
    color: var(--error);
    border-color: color-mix(in srgb, var(--error) 45%, var(--border-default));
  }

  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .error-bar {
    padding: var(--spacing-sm) var(--spacing-lg);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
    flex-shrink: 0;
  }
</style>
