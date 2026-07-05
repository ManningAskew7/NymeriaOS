<script lang="ts">
  import { fade, fly } from 'svelte/transition';
  import { trapFocus } from '$lib/actions/focus';
  import {
    OVERLAY_FADE_IN,
    OVERLAY_FADE_OUT,
    SHEET_RISE_IN,
    SHEET_RISE_OUT,
  } from '$lib/utils/transitions';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { errorsStore } from '$lib/stores/errors.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import { parseUserMessage } from '$lib/utils/messageParsing';
  import { rewindToMessage } from '$lib/utils/rewind';
  import { hapticNotification } from '$lib/utils/haptics';
  import Icon from '$lib/components/common/Icon.svelte';

  // Long-press on a user bubble opens this sheet (backlog #12). It resolves the
  // target from the live transcript by the id the store holds, so it always
  // reflects the current message even if the list re-renders under it.
  let message = $derived(
    chatStore.actionSheetMessageId
      ? chatStore.messages.find((m) => m.id === chatStore.actionSheetMessageId) ?? null
      : null
  );

  let rewinding = $state(false);

  // Auto-compact bubbles render a placeholder, not the stored prompt: editing
  // one would resend the literal placeholder string, so only Rewind is offered.
  let canEdit = $derived(
    message !== null && !parseUserMessage(message.content).isAutoCompact
  );

  function close() {
    chatStore.closeActionSheet();
  }

  function handleBackdropClick(e: MouseEvent) {
    if (e.target === e.currentTarget) close();
  }

  // Edit and resend: seed the composer with exactly the text the bubble shows
  // (metadata stripped) plus its image attachments. Rewinding drops images
  // from model context, so they must ride along on the resend; documents
  // persist in the thread sandbox and are left out. Skip images whose dataUrl
  // did not survive serialization.
  function handleEdit() {
    const target = message;
    if (!target) return;
    const images = (target.attachments || []).filter(
      (file) => file.type === 'image' && !!file.dataUrl
    );
    chatStore.beginEdit(target.id, parseUserMessage(target.content).text, images);
    close();
  }

  // Rewind to here: destructive, so confirm with the house window.confirm idiom
  // after a warning haptic. On failure surface a toast; on success the sheet
  // closes and rewindToMessage has already truncated the transcript.
  async function handleRewind() {
    const target = message;
    const threadId = threadsStore.currentThreadId;
    if (!target || !threadId || rewinding) return;

    await hapticNotification('warning');
    const ok = window.confirm(
      'Remove this message and everything after it? This cannot be undone.'
    );
    if (!ok) return;

    rewinding = true;
    const outcome = await rewindToMessage(threadId, target.id);
    rewinding = false;

    if (!outcome.ok) {
      errorsStore.push({
        kind: 'generic',
        message:
          outcome.reason === 'stale_target'
            ? 'The conversation changed and was reloaded.'
            : humanizeErrorText(outcome.error, {
                action: 'rewind',
                resource: 'the conversation',
              }),
      });
    }
    close();
  }

  // §6 a11y: Escape closes the sheet while open. Window listener so the
  // handler fires no matter where focus sits inside the sheet.
  $effect(() => {
    if (!message) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });
</script>

{#if message}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="sheet-backdrop" onclick={handleBackdropClick} in:fade={OVERLAY_FADE_IN} out:fade={OVERLAY_FADE_OUT}>
    <div
      class="action-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="Message actions"
      tabindex="-1"
      use:trapFocus
      in:fly={SHEET_RISE_IN}
      out:fly={SHEET_RISE_OUT}
    >
      <div class="sheet-handle" aria-hidden="true"></div>

      <div class="sheet-actions">
        {#if canEdit}
          <button class="sheet-item" type="button" onclick={handleEdit}>
            <Icon name="edit" size={18} />
            <span>Edit and resend</span>
          </button>
        {/if}

        <button class="sheet-item destructive" type="button" onclick={handleRewind} disabled={rewinding}>
          <Icon name="rewind" size={18} />
          <span>Rewind to here</span>
        </button>
      </div>

      <button class="sheet-cancel" type="button" onclick={close}>Cancel</button>
    </div>
  </div>
{/if}

<style>
  .sheet-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 1000;
    display: flex;
    align-items: flex-end;
    justify-content: center;
  }

  .action-sheet {
    width: 100%;
    background: var(--bg-elevated);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    padding: 8px var(--spacing-md) calc(var(--safe-area-bottom) + var(--spacing-md));
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
    max-height: 80dvh;
    overflow-y: auto;
  }

  .sheet-handle {
    align-self: center;
    width: 36px;
    height: 4px;
    border-radius: 2px;
    background: var(--border-default);
    margin-bottom: var(--spacing-sm);
  }

  .sheet-actions {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .sheet-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    min-height: var(--touch-target-min);
    padding: 14px var(--spacing-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-md);
    text-align: left;
    border-radius: var(--radius-sm);
    background: transparent;
    transition: background var(--transition-fast);
  }

  .sheet-item span {
    flex: 1;
  }

  .sheet-item:active {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .sheet-item.destructive {
    color: var(--error);
  }

  .sheet-item:disabled {
    opacity: 0.5;
  }

  .sheet-cancel {
    margin-top: var(--spacing-sm);
    min-height: var(--touch-target-min);
    padding: 14px;
    border-radius: var(--radius-md);
    background: var(--bg-base);
    color: var(--text-primary);
    font-size: var(--font-size-md);
    font-weight: 500;
    text-align: center;
  }

  .sheet-cancel:active {
    background: var(--bg-hover);
  }
</style>
