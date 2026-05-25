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
  import WizardShell from '$lib/components/common/WizardShell.svelte';

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

<WizardShell title={stepTitle} {onClose}>
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
      is starting its polling loop now (usually under 15 seconds).
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
</WizardShell>

<style>
  .muted {
    color: var(--text-muted);
  }

  .small {
    font-size: var(--font-size-sm);
  }

  .error {
    color: var(--error);
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
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    padding: 12px;
    min-height: var(--touch-target-min);
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
    font-family: var(--font-mono);
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
    min-height: var(--touch-target-min);
    background: var(--bg-elevated);
    color: var(--text-primary);
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
    min-height: var(--touch-target-min);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-weight: 500;
  }

  .action-btn.primary {
    background: var(--bg-elevated);
    color: var(--text-primary);
    border-color: var(--border-subtle);
  }

  .action-btn:active:not(:disabled) {
    background: var(--bg-hover);
  }

  .action-btn:disabled {
    opacity: 0.5;
  }
</style>
