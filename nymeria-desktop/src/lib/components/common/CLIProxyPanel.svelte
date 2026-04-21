<script lang="ts">
  import { cliproxyStore } from '$lib/stores/cliproxy.svelte';
  import { onMount, onDestroy } from 'svelte';
  import Button from './Button.svelte';

  onMount(() => {
    cliproxyStore.startPolling();
  });

  onDestroy(() => {
    cliproxyStore.stopPolling();
  });
</script>

<div class="tab-content">
  <!-- Status -->
  <div class="field">
    <span class="field-label">CLIProxy Status</span>
    <div class="status-row">
      <span class="status-dot" class:running={cliproxyStore.running} class:stopped={!cliproxyStore.running}></span>
      <span class="status-text">
        {#if cliproxyStore.running}
          Running on port 8317
        {:else}
          Stopped
        {/if}
      </span>
    </div>
    <p class="hint">CLIProxy routes LLM requests through your Claude/OpenAI subscription instead of API credits</p>
  </div>

  <!-- Start/Stop -->
  <div class="field">
    <div class="actions">
      {#if cliproxyStore.running}
        <Button variant="secondary" onclick={cliproxyStore.stop} disabled={cliproxyStore.loading}>
          {cliproxyStore.loading ? 'Stopping...' : 'Stop CLIProxy'}
        </Button>
      {:else}
        <Button variant="primary" onclick={cliproxyStore.start} disabled={cliproxyStore.loading}>
          {cliproxyStore.loading ? 'Starting...' : 'Start CLIProxy'}
        </Button>
      {/if}
    </div>
  </div>

  <!-- OAuth Sessions -->
  {#if cliproxyStore.running}
    <div class="field">
      <span class="field-label">Authenticated Sessions</span>
      {#if cliproxyStore.sessions.length > 0}
        <div class="sessions-list">
          {#each cliproxyStore.sessions as session}
            <div class="session-item">
              <span class="session-provider">{session.provider}</span>
              {#if session.email}
                <span class="session-email">{session.email}</span>
              {/if}
            </div>
          {/each}
        </div>
      {:else}
        <p class="hint warning">No active sessions. Log in with a provider below.</p>
      {/if}
    </div>

    <!-- Login Buttons -->
    <div class="field">
      <span class="field-label">Add Login</span>
      <div class="login-buttons">
        <Button variant="secondary" onclick={() => cliproxyStore.login('claude')}>
          Login with Claude
        </Button>
        <Button variant="secondary" onclick={() => cliproxyStore.login('openai')}>
          Login with OpenAI
        </Button>
      </div>
      <p class="hint">Opens your browser for OAuth authentication</p>
    </div>

    <!-- Quick Apply -->
    <div class="field">
      <span class="field-label">Route LLM Traffic</span>
      <div class="actions">
        <Button variant="primary" onclick={cliproxyStore.applyBaseUrl}>
          Use CLIProxy for LLM
        </Button>
        <Button variant="secondary" onclick={cliproxyStore.removeBaseUrl}>
          Use Direct API
        </Button>
      </div>
      <p class="hint">"Use CLIProxy" sets the LLM base URL to localhost:8317. "Use Direct API" removes the override.</p>
    </div>
  {/if}

  <!-- Error -->
  {#if cliproxyStore.error}
    <div class="error-message">
      {cliproxyStore.error}
    </div>
  {/if}
</div>

<style>
  .tab-content {
    padding: 1rem 0;
  }

  .field {
    margin-bottom: 1.25rem;
  }

  .field-label {
    display: block;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 0.5rem;
  }

  .hint {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0.25rem 0 0 0;
  }

  .hint.warning {
    color: var(--warning, #f59e0b);
  }

  .status-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .status-dot.running {
    background: var(--success, #22c55e);
    box-shadow: 0 0 6px var(--success, #22c55e);
  }

  .status-dot.stopped {
    background: var(--text-muted);
  }

  .status-text {
    font-size: 0.875rem;
    color: var(--text-secondary);
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .sessions-list {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .session-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: var(--bg-elevated-2);
    border-radius: 6px;
    border: 1px solid var(--border-default);
  }

  .session-provider {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-primary);
    text-transform: capitalize;
  }

  .session-email {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .login-buttons {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .error-message {
    padding: 0.75rem 1rem;
    background: rgba(239, 68, 68, 0.1);
    border: 1px solid var(--error, #ef4444);
    border-radius: 6px;
    font-size: 0.8125rem;
    color: var(--error, #ef4444);
  }
</style>
