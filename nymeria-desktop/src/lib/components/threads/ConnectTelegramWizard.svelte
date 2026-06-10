<script lang="ts">
  /**
   * Three-step wizard that walks the user through binding a desktop thread
   * to a Telegram chat.
   *
   *   1. link   — ensure the user's Telegram account is linked to their
   *               Nymeria account; auto-skipped if already linked.
   *   2. bind   — issue an 8-char bind code; user types `/bind <code>`
   *               (or taps the deep link) into the bot from the chat
   *               they want to use; we poll for the resulting binding row.
   *   3. done   — show the bound chat info and a Close button.
   */
  import { onDestroy, onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import type { ChatAppBinding, ChatAppBindCodeResponse } from '$lib/types';
  import Icon from '$lib/components/common/Icon.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import WizardShell from '$lib/components/common/WizardShell.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';

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
      errorMsg = humanizeErrorText(e, { action: 'load', resource: 'your Telegram link status' });
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
      errorMsg = humanizeErrorText(e, { action: 'create', resource: 'a link code' });
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
      errorMsg = humanizeErrorText(e, { action: 'create', resource: 'a bind code' });
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
        // Transient errors are fine — the bot may be momentarily slow.
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
        // Same as above.
      }
    }, 2000);
  }

  async function copyToClipboard(text: string) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Older browsers / no permission — silently ignore.
    }
  }

  function botMention(payload: ChatAppBindCodeResponse | null): string {
    return payload?.bot_username ? `@${payload.bot_username}` : 'your Telegram bot';
  }

  let stepTitle = $derived(
    currentStep === 'link'
      ? 'Step 1 of 2: Link your Telegram account'
      : currentStep === 'bind'
      ? (linkCode ? 'Step 2 of 2: Bind this thread to a chat' : 'Bind this thread to a chat')
      : 'Connected'
  );
</script>

<WizardShell title={stepTitle} {onClose}>
  {#if initializing}
    <p class="muted">Checking your Telegram link…</p>
  {:else if errorMsg}
    <div class="error">
      <p>{errorMsg}</p>
      <Button variant="secondary" onclick={onClose}>Close</Button>
    </div>
  {:else if currentStep === 'link'}
    <p>
      Your Telegram account isn't linked to NymeriaOS yet. Open your Telegram bot
      and send the link command. We'll detect it and continue automatically.
    </p>

    {#if linkCode}
      <div class="code-card">
        <div class="code-row">
          <code class="code-pill">{linkCode.code}</code>
          <Button variant="secondary" size="sm" onclick={() => copyToClipboard(linkCode!.code)}>Copy</Button>
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
          <Button variant="secondary" size="sm" onclick={() => copyToClipboard(bindCode!.code)}>Copy</Button>
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
            (e.g. a group), instead send <code>/bind {bindCode.code}</code>
            from inside that chat.
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
      <Button onclick={onClose}>Close</Button>
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

</style>
