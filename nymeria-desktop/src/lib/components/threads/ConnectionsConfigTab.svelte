<script lang="ts">
  import type { Thread, ThreadConfig } from '$lib/types';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import { notificationStore } from '$lib/stores/notifications.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import ConnectTelegramWizard from './ConnectTelegramWizard.svelte';
  import ConnectMyTelegramBotWizard from './ConnectMyTelegramBotWizard.svelte';
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';

  type TelegramAutonomousDelivery = ThreadConfig['telegramAutonomousDelivery'];
  type InAppNotificationLevel = ThreadConfig['inAppNotificationLevel'];

  /**
   * Connections tab: how this thread is exposed and reached. Publish it as a
   * callable tool, bind it to chat apps, and route its autonomous output and
   * notifications. (Notification routing used to be mislabeled under "Chat App".)
   */
  interface Props {
    thread: Thread;
    isCallable: boolean;
    callableName: string;
    callableDescription: string;
    telegramAutonomousDelivery: TelegramAutonomousDelivery;
    inAppNotificationLevel: InAppNotificationLevel;
    notificationProfile: string | null | undefined;
  }

  let {
    thread,
    isCallable = $bindable(),
    callableName = $bindable(),
    callableDescription = $bindable(),
    telegramAutonomousDelivery = $bindable(),
    inAppNotificationLevel = $bindable(),
    notificationProfile = $bindable(),
  }: Props = $props();

  let profilesLoaded = $state(false);
  $effect(() => {
    if (!profilesLoaded) {
      profilesLoaded = true;
      notificationStore.loadConfig().catch(() => {});
    }
  });

  function onProfileChange(event: Event) {
    const value = (event.target as HTMLSelectElement).value;
    notificationProfile = value === '' ? null : value;
  }

  let showChatAppWizard = $state(false);
  let showMyBotWizard = $state(false);
  let chatAppLoaded = $state(false);
  let chatAppLoadError = $state<string | null>(null);

  let chatAppBindings = $derived(chatAppBindingsStore.getBindings(thread.id));

  $effect(() => {
    if (!chatAppLoaded) {
      chatAppLoaded = true;
      chatAppBindingsStore.loadBindings(thread.id).catch((err) => {
        chatAppLoadError = err instanceof Error ? err.message : String(err);
      });
    }
  });

  async function handleUnbindChatApp(bindingId: number) {
    try {
      await chatAppBindingsStore.unbind(thread.id, bindingId);
      await threadsStore.syncFromBackend();
    } catch (err) {
      chatAppLoadError = err instanceof Error ? err.message : String(err);
    }
  }
</script>

<div class="tab-body">
  <ThreadSettingsSection
    title="Publish as Tool"
    icon="users"
    description="Mark this thread as a callable sub-agent so Nymeria can delegate tasks to it."
  >
    <label class="toggle-row">
      <input type="checkbox" bind:checked={isCallable} />
      <span class="toggle-label">Make callable</span>
    </label>

    {#if isCallable}
      <div class="field-group">
        <label class="field-label" for="callable-name-input">Callable Name</label>
        <input
          id="callable-name-input"
          class="field-input"
          type="text"
          bind:value={callableName}
          placeholder="e.g. ResearchAgent"
          maxlength={64}
        />
        <span class="field-hint">The tool name Nymeria uses to call this thread.</span>
      </div>

      <div class="field-group last">
        <label class="field-label" for="callable-desc-input">Callable Description</label>
        <textarea
          id="callable-desc-input"
          class="text-input"
          bind:value={callableDescription}
          placeholder="e.g. Autonomous web research that finds information, summarizes articles, and compiles reports"
          maxlength={500}
          rows={3}
        ></textarea>
        <span class="char-count">{callableDescription.length} / 500</span>
        <span class="field-hint">What the LLM sees as the tool description. Describe when to use this thread.</span>
      </div>
    {/if}
  </ThreadSettingsSection>

  <ThreadSettingsSection
    title="Notifications & Delivery"
    icon="bell"
    description="How this thread's autonomous output is delivered and where notifications route."
  >
    <div class="field-group">
      <label class="field-label" for="telegram-autonomous-delivery">Telegram autonomous output</label>
      <select id="telegram-autonomous-delivery" class="field-select" bind:value={telegramAutonomousDelivery}>
        <option value="full">Full output</option>
        <option value="notify_only">Notify only</option>
        <option value="off">Off</option>
      </select>
    </div>

    <div class="field-group">
      <label class="field-label" for="in-app-notification-level">Notification center</label>
      <select id="in-app-notification-level" class="field-select" bind:value={inAppNotificationLevel}>
        <option value="notify_only">Notify only</option>
        <option value="all_autonomous">All autonomous completions</option>
        <option value="off">Off</option>
      </select>
    </div>

    <div class="field-group last">
      <label class="field-label" for="notification-profile">Notification profile override</label>
      <select id="notification-profile" class="field-select" value={notificationProfile ?? ''} onchange={onProfileChange}>
        <option value="">
          Use account default ({notificationStore.preferences?.defaultProfile ?? 'default'})
        </option>
        {#each notificationStore.profiles as p (p.id)}
          <option value={p.name}>{p.name}</option>
        {/each}
      </select>
      <span class="field-hint">
        Which destination profile the <code>notify</code> tool routes through on this thread.
        Manage profiles in Settings → Notifications.
      </span>
    </div>
  </ThreadSettingsSection>

  <ThreadSettingsSection
    title="Chat Apps"
    icon="chat"
    description="Bind this thread to a chat-app conversation so messages flow both ways. You can keep using the desktop app for the same thread."
    flush
  >
    <div class="chatapp-body">
      {#if chatAppLoadError}
        <div class="error-bar">{chatAppLoadError}</div>
      {/if}

      {#if chatAppBindings.length === 0}
        <div class="chatapp-cta">
          <p style="margin: 0;">No chats bound to this thread yet. Pick how you want to connect:</p>
          <div class="chatapp-cta-buttons">
            <button class="btn btn-primary" type="button" onclick={() => (showChatAppWizard = true)}>Connect via shared bot</button>
            <button class="btn btn-secondary" type="button" onclick={() => (showMyBotWizard = true)}>Use my own bot</button>
          </div>
          <p class="field-hint" style="margin: 0.25rem 0 0;">
            <strong>Shared:</strong> use the existing Nymeria bot for the fastest setup, with no BotFather required.
            <br />
            <strong>My own bot:</strong> paste a token from <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> for a branded bot you control. Requires <code>NYMERIA_SECRETS_KEY</code> on the server.
          </p>
        </div>
      {:else}
        <ul class="binding-list">
          {#each chatAppBindings as binding (binding.id)}
            <li class="binding-row">
              <div class="binding-meta">
                <span class="binding-provider">{binding.provider}</span>
                <code class="binding-chat">chat {binding.platform_chat_id}</code>
                <span class="binding-when">
                  via {binding.user_telegram_bot_id ? 'your bot' : 'shared bot'}
                  &middot; since {new Date(binding.created_at).toLocaleString()}
                </span>
              </div>
              <button class="btn btn-ghost" type="button" onclick={() => handleUnbindChatApp(binding.id)}>Unbind</button>
            </li>
          {/each}
        </ul>
        <p class="field-hint" style="margin-top: 0.5rem;">Only one chat per thread at a time. Unbind first to switch.</p>
      {/if}
    </div>
  </ThreadSettingsSection>
</div>

{#if showChatAppWizard}
  <div class="chatapp-wizard-backdrop">
    <div class="chatapp-wizard-card">
      <ConnectTelegramWizard
        threadId={thread.id}
        onClose={() => (showChatAppWizard = false)}
        onBound={() => {
          chatAppBindingsStore.loadBindings(thread.id).catch(() => {});
          threadsStore.syncFromBackend().catch(() => {});
        }}
      />
    </div>
  </div>
{/if}

{#if showMyBotWizard}
  <div class="chatapp-wizard-backdrop">
    <div class="chatapp-wizard-card">
      <ConnectMyTelegramBotWizard
        threadId={thread.id}
        onClose={() => (showMyBotWizard = false)}
        onBound={() => {
          chatAppBindingsStore.loadBindings(thread.id).catch(() => {});
          threadsStore.syncFromBackend().catch(() => {});
        }}
      />
    </div>
  </div>
{/if}

<style>
  .tab-body {
    padding: var(--spacing-lg);
  }

  .field-group { margin-bottom: var(--spacing-md); }
  .field-group.last { margin-bottom: 0; }

  .field-label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 4px;
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: var(--spacing-xs) 0 0;
  }

  .field-input,
  .field-select,
  .text-input {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }
  .text-input { font-family: inherit; resize: vertical; }
  .field-select { cursor: pointer; }
  .field-input:focus,
  .field-select:focus,
  .text-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }
  .field-input::placeholder,
  .text-input::placeholder { color: var(--text-muted); }

  .char-count {
    display: block;
    text-align: right;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-top: 6px;
  }

  .toggle-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-md) 0;
    cursor: pointer;
  }
  .toggle-row input[type='checkbox'] {
    width: 16px;
    height: 16px;
    accent-color: var(--accent-primary);
    cursor: pointer;
  }
  .toggle-label {
    font-size: var(--font-size-sm);
    line-height: 1.4;
    color: var(--text-primary);
  }

  .chatapp-body { padding: var(--spacing-md); }

  .error-bar {
    padding: var(--spacing-sm) var(--spacing-md);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
    border-radius: var(--radius-sm);
    margin-bottom: var(--spacing-sm);
  }

  .btn {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }
  .btn-primary:hover:not(:disabled) { filter: brightness(1.1); }
  .btn-secondary {
    color: var(--text-primary);
    background: transparent;
    border: 1px solid var(--border-default);
  }
  .btn-secondary:hover:not(:disabled) { background: var(--bg-hover); }
  .btn-ghost {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
  }
  .btn-ghost:hover:not(:disabled) { color: var(--text-primary); background: var(--bg-hover); }

  .binding-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }
  .binding-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-base);
  }
  .binding-meta { display: flex; flex-direction: column; gap: 2px; }
  .binding-provider { font-weight: 600; text-transform: capitalize; font-size: var(--font-size-sm); }
  .binding-chat { font-family: var(--font-mono); font-size: var(--font-size-sm); }
  .binding-when { color: var(--text-muted); font-size: var(--font-size-xs); }

  .chatapp-cta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-muted);
    text-align: left;
  }
  .chatapp-cta-buttons { display: flex; gap: var(--spacing-sm); flex-wrap: wrap; }

  .chatapp-wizard-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1100;
  }
  .chatapp-wizard-card {
    background: var(--glass-bg-strong);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    box-shadow: 0 24px 48px rgba(0, 0, 0, 0.4);
    overflow: hidden;
  }
</style>
