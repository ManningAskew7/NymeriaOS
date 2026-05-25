<script lang="ts">
  /**
   * BYO (bring-your-own) Telegram-bot wizard. Sibling of
   * ``ConnectTelegramWizard.svelte`` — that one binds the shared global
   * @NymeriaaaaaBot to a chat; this one registers the user's own bot
   * (via a BotFather token) and binds it.
   *
   * Steps:
   *   1. token   — paste the BotFather token. Server validates via getMe,
   *                encrypts at rest, registers the bot.
   *   2. starting — supervisor picks up the new bot within ~15s.
   *   3. bind    — issue a thread-bind code; user types `/bind <code>`
   *                in their own bot from the chat they want bound.
   *   4. done    — show the binding and a Close button.
   */
  import { onDestroy, onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import type {
    ChatAppBinding,
    ChatAppBindCodeResponse,
    MyTelegramBot
  } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import Button from '$lib/components/common/Button.svelte';
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
      // Fine — fall back to token step.
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
        // transient; we'll retry on the next tick
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
        // transient
      }
    }, 2000);
  }

  async function copyToClipboard(text: string) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // older browsers — code is on screen for manual copy
    }
  }

  let stepTitle = $derived(
    currentStep === 'token'
      ? 'Step 1 of 3: Paste your bot token'
      : currentStep === 'starting'
      ? 'Step 2 of 3: Starting your bot…'
      : currentStep === 'bind'
      ? 'Step 3 of 3: Bind a chat to this thread'
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
      token it gives you. Paste it below; the server validates it via Telegram
      and stores it encrypted.
    </p>

    <label class="field">
      <span class="field-label-inline">Bot token</span>
      <input
        type="password"
        class="token-input"
        bind:value={botToken}
        placeholder="123456789:ABCdef-GhiJklMnoPqrsTuvWxyZ-1234567890"
        autocomplete="off"
        spellcheck="false"
        disabled={busy}
      />
    </label>

    <p class="muted small">
      The token is encrypted with the server's <code>NYMERIA_SECRETS_KEY</code>
      before it touches the database. The plaintext is never returned in any
      API response. To revoke, use BotFather's <code>/revoke</code>. That
      rotates the token on Telegram's side; the stored ciphertext stops
      working immediately.
    </p>

    <div class="actions">
      <Button
        disabled={busy || !botToken.trim()}
        loading={busy}
        onclick={handleSubmitToken}
      >{busy ? 'Validating…' : 'Continue'}</Button>
    </div>
  {:else if currentStep === 'starting'}
    <p>
      Your bot is registered as
      <strong>@{registeredBot?.bot_username ?? '…'}</strong>. The supervisor
      process is starting its polling loop now. This normally takes
      15 seconds or less.
    </p>
    <div class="muted small">
      Waiting for the bot to come online…
    </div>
  {:else if currentStep === 'bind'}
    <p>
      <strong>@{registeredBot?.bot_username}</strong> is alive. Open Telegram,
      find this bot, and bind a chat to this thread:
    </p>

    {#if bindCode}
      <div class="code-card">
        <div class="code-row">
          <code class="code-pill">{bindCode.code}</code>
          <Button variant="secondary" size="sm" onclick={() => copyToClipboard(bindCode!.code)}>Copy</Button>
        </div>

        {#if bindDeepLink}
          <a
            class="deep-link"
            href={bindDeepLink}
            target="_blank"
            rel="noopener noreferrer"
          >Open @{registeredBot?.bot_username} and bind this chat</a>
          <p class="muted small">
            The deep link binds your private DM with the bot. To bind a
            different chat (e.g. a group), instead send
            <code>/bind {bindCode.code}</code> from inside that chat.
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
        bound to this thread, served by your bot
        <strong>@{registeredBot?.bot_username}</strong>. Messages sent there
        will appear here, and replies will stream both ways.
      </p>
      <Button onclick={onClose}>Done</Button>
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

  .field-label-inline {
    font-weight: 500;
    font-size: var(--font-size-sm);
  }

  .token-input {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
    padding: 0.5rem 0.7rem;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
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
    display: inline-block;
    align-self: flex-start;
    padding: 0.5rem 0.9rem;
    background: var(--bg-elevated);
    color: var(--text-primary);
    border-radius: var(--radius-sm);
    text-decoration: none;
    font-weight: 500;
    border: 1px solid var(--border-subtle);
  }

  .deep-link:hover {
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
</style>
