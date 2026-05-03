<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import type {
    ChatAppBinding,
    ChatAppBindCodeResponse,
    MyTelegramBot
  } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';

  type Step = 'token' | 'starting' | 'bind' | 'done';

  interface Props {
    threadId: string;
    onClose: () => void;
    onBound?: (binding: ChatAppBinding) => void;
  }

  let { threadId, onClose, onBound }: Props = $props();

  let currentStep = $state<Step>('token');
  let errorMsg = $state<string | null>(null);
  let busy = $state(false);

  let botToken = $state('');
  let registeredBot = $state<MyTelegramBot | null>(null);
  let bindCode = $state<ChatAppBindCodeResponse | null>(null);
  let resultBinding = $state<ChatAppBinding | null>(null);

  let pollHandle: ReturnType<typeof setInterval> | null = null;

  function stopPolling() {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  onMount(async () => {
    try {
      const existing = await api.listMyTelegramBots();
      if (existing.length > 0) {
        registeredBot = existing[0];
        if (registeredBot.last_seen_at) {
          await issueBindCode();
          currentStep = 'bind';
          startBindPoll();
        } else {
          currentStep = 'starting';
          startStartingPoll();
        }
      }
    } catch {
      // Fall back to token step.
    }
  });

  onDestroy(stopPolling);

  async function handleSubmitToken() {
    const trimmed = botToken.trim();
    if (!trimmed) {
      errorMsg = 'Paste your bot token first.';
      return;
    }
    busy = true;
    errorMsg = null;
    try {
      const bot = await api.registerMyTelegramBot(trimmed);
      registeredBot = bot;
      botToken = '';
      if (bot.last_seen_at) {
        await issueBindCode();
        currentStep = 'bind';
        startBindPoll();
      } else {
        currentStep = 'starting';
        startStartingPoll();
      }
    } catch (e) {
      errorMsg = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  function startStartingPoll() {
    stopPolling();
    pollHandle = setInterval(async () => {
      if (!registeredBot) return;
      try {
        const fresh = await api.getMyTelegramBot(registeredBot.id);
        registeredBot = fresh;
        if (fresh.last_seen_at) {
          stopPolling();
          await issueBindCode();
          currentStep = 'bind';
          startBindPoll();
        }
      } catch {
        // Transient — retry on next tick.
      }
    }, 2000);
  }

  async function issueBindCode() {
    try {
      bindCode = await api.issueChatAppBindCode(threadId, 'telegram');
    } catch (e) {
      errorMsg = e instanceof Error ? e.message : String(e);
    }
  }

  function startBindPoll() {
    stopPolling();
    pollHandle = setInterval(async () => {
      try {
        const bindings = await chatAppBindingsStore.loadBindings(threadId);
        const found = bindings.find(
          (b) =>
            b.provider === 'telegram' &&
            b.user_telegram_bot_id === registeredBot?.id
        );
        if (found) {
          stopPolling();
          resultBinding = found;
          currentStep = 'done';
          onBound?.(found);
        }
      } catch {
        // Transient — retry on next tick.
      }
    }, 2000);
  }

  async function copyToClipboard(text: string) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Code is on screen for manual copy.
    }
  }

  let stepTitle = $derived(
    currentStep === 'token'
      ? 'Step 1 of 3: Paste bot token'
      : currentStep === 'starting'
      ? 'Step 2 of 3: Starting your bot…'
      : currentStep === 'bind'
      ? 'Step 3 of 3: Bind a chat'
      : 'Connected'
  );

  let bindDeepLink = $derived(
    bindCode && registeredBot
      ? `https://t.me/${registeredBot.bot_username}?start=bind_${bindCode.code}`
      : null
  );
</script>

<div class="wizard-backdrop" role="presentation" onclick={onClose}>
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="wizard" onclick={(e) => e.stopPropagation()}>
    <header class="wizard-header">
      <button class="back-btn" type="button" onclick={onClose} aria-label="Close">
        <Icon name="chevronLeft" size={22} />
      </button>
      <h3>{stepTitle}</h3>
    </header>

    <div class="wizard-body">
      {#if errorMsg}
        <div class="error">
          <p>{errorMsg}</p>
        </div>
      {/if}

      {#if currentStep === 'token'}
        <p>
          Open <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>
          in Telegram, send <code>/newbot</code>, follow the prompts, and copy the
          token it gives you. Paste it below.
        </p>

        <label class="field">
          <span class="field-label">Bot token</span>
          <input
            type="password"
            class="token-input"
            bind:value={botToken}
            placeholder="123456789:ABCdef..."
            autocomplete="off"
            spellcheck="false"
            disabled={busy}
          />
        </label>

        <p class="muted small">
          The token is encrypted before storage. The plaintext is never
          returned in any API response. Revoke via BotFather's <code>/revoke</code>.
        </p>

        <div class="actions">
          <button
            class="action-btn primary"
            type="button"
            disabled={busy || !botToken.trim()}
            onclick={handleSubmitToken}
          >{busy ? 'Validating…' : 'Continue'}</button>
        </div>
      {:else if currentStep === 'starting'}
        <p>
          Your bot is registered as
          <strong>@{registeredBot?.bot_username ?? '…'}</strong>. The server
          is starting its polling loop now — usually under 15 seconds.
        </p>
        <div class="muted small">
          Waiting for the bot to come online…
        </div>
      {:else if currentStep === 'bind'}
        <p>
          <strong>@{registeredBot?.bot_username}</strong> is alive. Open Telegram
          and bind a chat to this thread:
        </p>

        {#if bindCode}
          <div class="code-card">
            <div class="code-row">
              <code class="code-pill">{bindCode.code}</code>
              <button
                class="action-btn secondary"
                type="button"
                onclick={() => copyToClipboard(bindCode!.code)}
              >Copy</button>
            </div>

            {#if bindDeepLink}
              <a
                class="deep-link"
                href={bindDeepLink}
                target="_blank"
                rel="noopener noreferrer"
              >Open @{registeredBot?.bot_username} and bind</a>
              <p class="muted small">
                The deep link binds your DM with the bot. To bind a
                different chat, send <code>/bind {bindCode.code}</code>
                from inside that chat instead.
              </p>
            {:else}
              <p class="muted small">
                In the chat you want to bind, send
                <code>/bind {bindCode.code}</code> to @{registeredBot?.bot_username}.
              </p>
            {/if}
          </div>

          <p class="muted small">Code expires in 10 minutes. Waiting for confirmation…</p>
        {:else}
          <p class="muted">Issuing code…</p>
        {/if}
      {:else if currentStep === 'done' && resultBinding}
        <div class="success">
          <Icon name="check" size={28} />
          <h4>Connected</h4>
          <p>
            Telegram chat <code>{resultBinding.platform_chat_id}</code> is now
            bound to this thread via your bot
            <strong>@{registeredBot?.bot_username}</strong>. Messages will
            stream both ways.
          </p>
          <button class="action-btn primary" type="button" onclick={onClose}>Done</button>
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .wizard-backdrop {
    position: fixed;
    inset: 0;
    z-index: 400;
    background: var(--bg-base);
    display: flex;
    flex-direction: column;
  }

  .wizard {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .wizard-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-md);
    height: var(--header-height, 56px);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    padding-top: env(safe-area-inset-top);
  }

  .back-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    background: transparent;
    border: none;
  }

  .back-btn:active {
    background: var(--bg-hover);
  }

  .wizard-header h3 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    flex: 1;
  }

  .wizard-body {
    padding: var(--spacing-lg);
    overflow-y: auto;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding-bottom: calc(var(--spacing-lg) + env(safe-area-inset-bottom));
  }

  .wizard-body p {
    margin: 0;
    line-height: 1.5;
  }

  .muted {
    color: var(--text-muted);
  }

  .small {
    font-size: var(--font-size-sm);
  }

  .error {
    color: var(--color-error, #d33);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .field-label {
    font-weight: 500;
    font-size: var(--font-size-sm);
  }

  .token-input {
    font-family: var(--font-family-mono);
    font-size: var(--font-size-sm);
    padding: 12px;
    min-height: 44px;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-base);
    color: var(--text-primary);
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
  }

  .code-card {
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .code-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
  }

  .code-pill {
    font-family: var(--font-family-mono);
    font-size: var(--font-size-lg);
    font-weight: 600;
    letter-spacing: 0.1em;
    background: var(--bg-base);
    padding: 0.4rem 0.8rem;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-subtle);
  }

  .deep-link {
    display: block;
    text-align: center;
    padding: 12px 16px;
    min-height: 44px;
    background: var(--accent-bg, var(--bg-elevated));
    color: var(--accent-fg, var(--text-primary));
    border-radius: var(--radius-md);
    text-decoration: none;
    font-weight: 500;
    border: 1px solid var(--border-subtle);
  }

  .deep-link:active {
    background: var(--bg-hover);
  }

  .success {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--spacing-sm);
  }

  .success h4 {
    margin: 0;
  }

  .action-btn {
    padding: 12px 20px;
    min-height: 44px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-weight: 500;
  }

  .action-btn.primary {
    background: var(--accent-bg, var(--bg-elevated));
    color: var(--accent-fg, var(--text-primary));
    border-color: var(--accent-bg, var(--border-subtle));
  }

  .action-btn:active:not(:disabled) {
    background: var(--bg-hover);
  }

  .action-btn:disabled {
    opacity: 0.5;
  }
</style>
