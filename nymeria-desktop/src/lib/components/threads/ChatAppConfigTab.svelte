<script lang="ts">
  import type { Thread, ThreadConfig } from '$lib/types';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import ConnectTelegramWizard from './ConnectTelegramWizard.svelte';
  import ConnectMyTelegramBotWizard from './ConnectMyTelegramBotWizard.svelte';

  type TelegramAutonomousDelivery = ThreadConfig['telegramAutonomousDelivery'];
  type InAppNotificationLevel = ThreadConfig['inAppNotificationLevel'];

  interface Props {
    thread: Thread;
    telegramAutonomousDelivery: TelegramAutonomousDelivery;
    inAppNotificationLevel: InAppNotificationLevel;
  }

  let {
    thread,
    telegramAutonomousDelivery = $bindable(),
    inAppNotificationLevel = $bindable(),
  }: Props = $props();

  let showChatAppWizard = $state(false);
  let showMyBotWizard = $state(false);
  let chatAppLoaded = $state(false);
  let chatAppLoadError = $state<string | null>(null);

  let chatAppBindings = $derived(chatAppBindingsStore.getBindings(thread.id));

  $effect(() => {
    if (!chatAppLoaded) {
      chatAppLoaded = true;
      chatAppBindingsStore
        .loadBindings(thread.id)
        .catch((err) => {
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

<div class="tab-panel">
  <p class="field-hint">
    Bind this thread to a chat-app conversation so messages flow both ways.
    You can keep using the desktop app for the same thread; nothing changes
    here when you chat from the bound chat instead.
  </p>

  <div class="visibility-section">
    <h3 class="section-title">Attention & Delivery</h3>

    <label class="field-label" for="telegram-autonomous-delivery">
      Telegram autonomous output
    </label>
    <select
      id="telegram-autonomous-delivery"
      class="field-input"
      bind:value={telegramAutonomousDelivery}
    >
      <option value="full">Full output</option>
      <option value="notify_only">Notify only</option>
      <option value="off">Off</option>
    </select>

    <label class="field-label" for="in-app-notification-level">
      Notification center
    </label>
    <select
      id="in-app-notification-level"
      class="field-input"
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
      <p style="margin: 0;">No chats bound to this thread yet. Pick how you want to connect:</p>
      <div class="chatapp-cta-buttons">
        <button
          class="btn btn-primary"
          type="button"
          onclick={() => (showChatAppWizard = true)}
        >Connect via shared bot</button>
        <button
          class="btn btn-secondary"
          type="button"
          onclick={() => (showMyBotWizard = true)}
        >Use my own bot</button>
      </div>
      <p class="field-hint" style="margin: 0.25rem 0 0;">
        <strong>Shared:</strong> use the existing Nymeria bot for the
        fastest setup, with no BotFather required.
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
          <button
            class="btn btn-ghost"
            type="button"
            onclick={() => handleUnbindChatApp(binding.id)}
          >Unbind</button>
        </li>
      {/each}
    </ul>
    <p class="field-hint" style="margin-top: 0.5rem;">
      Only one chat per thread at a time. Unbind first to switch.
    </p>
  {/if}
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
  .tab-panel {
    padding: var(--spacing-lg);
  }

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
    margin: 0 0 var(--spacing-sm) 0;
  }

  .field-input {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
    cursor: pointer;
  }

  .field-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .visibility-section {
    margin-bottom: var(--spacing-lg);
    padding-bottom: var(--spacing-md);
    border-bottom: 1px solid var(--border-default);
  }

  .section-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 var(--spacing-xs) 0;
  }

  .error-bar {
    padding: var(--spacing-sm) var(--spacing-lg);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
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

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-secondary {
    color: var(--text-primary);
    background: transparent;
    border: 1px solid var(--border-default);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--bg-hover);
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
    background: var(--bg-elevated);
  }

  .binding-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .binding-provider {
    font-weight: 600;
    text-transform: capitalize;
    font-size: var(--font-size-sm);
  }

  .binding-chat {
    font-family: var(--font-family-mono);
    font-size: var(--font-size-sm);
  }

  .binding-when {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .chatapp-cta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding: var(--spacing-lg);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-muted);
    text-align: left;
  }

  .chatapp-cta-buttons {
    display: flex;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

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
