<script lang="ts">
  import { Icon } from '$lib/components/common';
  import type { Notification } from '$lib/types';

  interface Props {
    notification: Notification;
    onclick?: () => void;
    onDismiss?: () => void;
    onDelete?: () => void;
  }

  let { notification, onclick, onDismiss, onDelete }: Props = $props();

  let hasErrors = $derived(
    notification.errors && Object.keys(notification.errors).length > 0
  );
  let errorEntries = $derived(
    notification.errors ? Object.entries(notification.errors) : []
  );

  function formatTimeAgo(date: Date): string {
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / (1000 * 60));
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays === 1) return 'yesterday';
    return `${diffDays}d ago`;
  }

  function handleClick() {
    onclick?.();
  }

  function handleDismiss(e: MouseEvent) {
    e.stopPropagation();
    onDismiss?.();
  }

  function handleDelete(e: MouseEvent) {
    e.stopPropagation();
    onDelete?.();
  }
</script>

<button
  class="notification-item"
  class:unread={!notification.read}
  class:has-errors={hasErrors}
  type="button"
  onclick={handleClick}
>
  <div class="notification-content">
    <div class="notification-icon">
      <Icon name="bell" size={16} />
    </div>
    <div class="notification-text">
      <p class="notification-summary">{notification.summary}</p>
      <div class="notification-meta">
        <span class="notification-time">{formatTimeAgo(notification.createdAt)}</span>
        {#if notification.profile}
          <span class="meta-sep">·</span>
          <span class="notification-profile" title="Profile used for routing">
            {notification.profile}
          </span>
        {/if}
      </div>
      {#if notification.deliveredTo.length > 0 || hasErrors}
        <div class="delivery-row">
          {#each notification.deliveredTo as dest (dest)}
            <span class="delivery-badge ok" title="Delivered to {dest}">
              <Icon name="check" size={10} />
              {dest}
            </span>
          {/each}
          {#each errorEntries as [dest, err] (dest)}
            <span class="delivery-badge err" title="{dest}: {err}">
              <Icon name="x" size={10} />
              {dest}
            </span>
          {/each}
        </div>
      {/if}
    </div>
  </div>
  <div class="row-actions">
    {#if !notification.read && onDismiss}
      <span
        class="row-action"
        role="button"
        tabindex="0"
        onclick={handleDismiss}
        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleDismiss(e as unknown as MouseEvent); } }}
        title="Mark as read"
      >
        <Icon name="check" size={14} />
      </span>
    {/if}
    {#if onDelete}
      <span
        class="row-action delete"
        role="button"
        tabindex="0"
        onclick={handleDelete}
        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleDelete(e as unknown as MouseEvent); } }}
        title="Delete notification"
      >
        <Icon name="x" size={14} />
      </span>
    {/if}
  </div>
</button>

<style>
  .notification-item {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--spacing-xs);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    text-align: left;
    background: transparent;
    border-radius: var(--radius-md);
    transition: background var(--transition-fast);
    cursor: pointer;
  }

  .notification-item:hover {
    background: var(--bg-hover);
  }

  .notification-item.unread {
    background: var(--bg-elevated);
  }

  .notification-item.unread:hover {
    background: var(--bg-hover);
  }

  .notification-item.has-errors {
    border-left: 2px solid var(--error);
  }

  .notification-content {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    flex: 1;
    min-width: 0;
  }

  .notification-icon {
    flex-shrink: 0;
    color: var(--accent-primary);
    margin-top: 2px;
  }

  .notification-text {
    flex: 1;
    min-width: 0;
  }

  .notification-summary {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    line-height: 1.4;
    word-wrap: break-word;
  }

  .unread .notification-summary {
    font-weight: 500;
  }

  .notification-meta {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 2px;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .meta-sep {
    opacity: 0.6;
  }

  .notification-profile {
    font-variant: small-caps;
    letter-spacing: 0.02em;
  }

  .delivery-row {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 6px;
  }

  .delivery-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding: 2px 6px;
    border-radius: var(--radius-sm);
    font-size: var(--font-size-3xs);
    line-height: 1;
    background: var(--bg-elevated);
    color: var(--text-muted);
  }

  .delivery-badge.ok {
    background: color-mix(in srgb, var(--accent-primary) 12%, transparent);
    color: var(--accent-primary);
  }

  .delivery-badge.err {
    background: color-mix(in srgb, var(--error) 14%, transparent);
    color: var(--error);
  }

  .row-actions {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 2px;
    opacity: 0;
  }

  .notification-item:hover .row-actions {
    opacity: 1;
  }

  .row-action {
    padding: var(--spacing-xs);
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
    cursor: pointer;
  }

  .row-action:hover {
    background: var(--bg-elevated);
    color: var(--text-primary);
  }

  .row-action.delete:hover {
    color: var(--error);
  }

  /* The actions are opacity:0 until the row is hovered. A keyboard user who
     Tabs into one would otherwise focus an invisible control: reveal the
     group on focus-within so the global :focus-visible ring is actually
     seen. The ring itself comes from the app.css baseline. */
  .row-actions:focus-within {
    opacity: 1;
  }
</style>
