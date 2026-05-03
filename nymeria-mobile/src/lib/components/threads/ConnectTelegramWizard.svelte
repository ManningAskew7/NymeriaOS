<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import type { ChatAppBinding, ChatAppBindCodeResponse } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';

  type Step = 'link' | 'bind' | 'done';

  interface Props {
    threadId: string;
    provider?: 'telegram';
    onClose: () => void;
    onBound?: (binding: ChatAppBinding) => void;
  }

  let { threadId, provider = 'telegram', onClose, onBound }: Props = $props();

  let currentStep = $state<Step>('link');
  let initializing = $state(true);
  let issuing = $state(false);
  let errorMsg = $state<string | null>(null);

  let linkCode = $state<ChatAppBindCodeResponse | null>(null);
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
      const platforms = await api.listMyPlatforms();
      const linked = platforms.some((p) => p.provider === provider);
      if (linked) {
        await advanceToBindStep();
      } else {
        await issueLinkCode();
        currentStep = 'link';
        startLinkPoll();
      }
    } catch (e) {
      errorMsg = e instanceof Error ? e.message : String(e);
    } finally {
      initializing = false;
    }
  });

  onDestroy(stopPolling);

  async function issueLinkCode() {
    issuing = true;
    errorMsg = null;
    try {
      linkCode = await api.requestSelfPlatformLinkCode(provider);
    } catch (e) {
      errorMsg = e instanceof Error ? e.message : String(e);
    } finally {
      issuing = false;
    }
  }

  async function issueBindCode() {
    issuing = true;
    errorMsg = null;
    try {
      bindCode = await api.issueChatAppBindCode(threadId, provider);
    } catch (e) {
      errorMsg = e instanceof Error ? e.message : String(e);
    } finally {
      issuing = false;
    }
  }

  async function advanceToBindStep() {
    await issueBindCode();
    currentStep = 'bind';
    startBindPoll();
  }

  function startLinkPoll() {
    stopPolling();
    pollHandle = setInterval(async () => {
      try {
        const platforms = await api.listMyPlatforms();
        if (platforms.some((p) => p.provider === provider)) {
          stopPolling();
          await advanceToBindStep();
        }
      } catch {
        // Transient errors — retry on next tick.
      }
    }, 2000);
  }

  function startBindPoll() {
    stopPolling();
    pollHandle = setInterval(async () => {
      try {
        const bindings = await chatAppBindingsStore.loadBindings(threadId);
        const found = bindings.find((b) => b.provider === provider);
        if (found) {
          stopPolling();
          resultBinding = found;
          currentStep = 'done';
          onBound?.(found);
        }
      } catch {
        // Transient errors — retry on next tick.
      }
    }, 2000);
  }

  async function copyToClipboard(text: string) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Older browsers / no permission — code is on screen for manual copy.
    }
  }

  function botMention(payload: ChatAppBindCodeResponse | null): string {
    return payload?.bot_username ? `@${payload.bot_username}` : 'your Telegram bot';
  }

  let stepTitle = $derived(
    currentStep === 'link'
      ? 'Step 1 of 2: Link your Telegram'
      : currentStep === 'bind'
      ? (linkCode ? 'Step 2 of 2: Bind to a chat' : 'Bind this thread to a chat')
      : 'Connected'
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
      {#if initializing}
        <p class="muted">Checking your Telegram link…</p>
      {:else if errorMsg}
        <div class="error">
          <p>{errorMsg}</p>
          <button class="action-btn secondary" type="button" onclick={onClose}>Close</button>
        </div>
      {:else if currentStep === 'link'}
        <p>
          Your Telegram account isn't linked to Nymeria yet. Open your Telegram bot
          and send the link command. We'll detect it automatically.
        </p>

        {#if linkCode}
          <div class="code-card">
            <div class="code-row">
              <code class="code-pill">{linkCode.code}</code>
              <button
                class="action-btn secondary"
                type="button"
                onclick={() => copyToClipboard(linkCode!.code)}
              >Copy</button>
            </div>

            {#if linkCode.deep_link}
              <a
                class="deep-link"
                href={linkCode.deep_link}
                target="_blank"
                rel="noopener noreferrer"
              >Open Telegram and link account</a>
            {:else}
              <p class="muted small">
                In Telegram, send <code>/start link_{linkCode.code}</code> to {botMention(linkCode)}.
              </p>
            {/if}
          </div>

          <p class="muted small">Code expires in 10 minutes. Waiting for confirmation…</p>
        {:else if issuing}
          <p class="muted">Issuing code…</p>
        {/if}
      {:else if currentStep === 'bind'}
        <p>
          Your account is linked. Now open the Telegram chat you want to use
          for this thread and tell the bot to bind it.
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

            {#if bindCode.deep_link}
              <a
                class="deep-link"
                href={bindCode.deep_link}
                target="_blank"
                rel="noopener noreferrer"
              >Open the bot and bind this chat</a>
              <p class="muted small">
                The deep link opens a DM with the bot. To bind a different chat
                (e.g. a group), send <code>/bind {bindCode.code}</code>
                from inside that chat instead.
              </p>
            {:else}
              <p class="muted small">
                In the chat you want to bind, send <code>/bind {bindCode.code}</code>
                to {botMention(bindCode)}.
              </p>
            {/if}
          </div>

          <p class="muted small">Code expires in 10 minutes. Waiting for confirmation…</p>
        {:else if issuing}
          <p class="muted">Issuing code…</p>
        {/if}
      {:else if currentStep === 'done' && resultBinding}
        <div class="success">
          <Icon name="check" size={28} />
          <h4>Connected</h4>
          <p>
            Telegram chat <code>{resultBinding.platform_chat_id}</code> is now bound
            to this thread. Messages sent there will appear here, and replies will
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
    display: flex;
    flex-direction: column;
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

  .action-btn:active {
    background: var(--bg-hover);
  }
</style>
