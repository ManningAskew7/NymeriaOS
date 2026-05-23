<script lang="ts">
  import { backendProcessStore } from '$lib/stores/backendProcess.svelte';

  let dots = $state('');

  // Animate dots while starting
  const interval = setInterval(() => {
    dots = dots.length >= 3 ? '' : dots + '.';
  }, 500);

  $effect(() => {
    if (backendProcessStore.isReady) {
      clearInterval(interval);
    }
  });
</script>

<div class="startup-overlay">
  <div class="startup-content">
    <div class="logo-container">
      <div class="spinner" class:error={backendProcessStore.status === 'failed'}></div>
    </div>

    {#if backendProcessStore.status === 'starting'}
      <h1 class="startup-title">Starting Nymeria{dots}</h1>
      <p class="startup-subtitle">Initializing backend services</p>
    {:else if backendProcessStore.status === 'failed'}
      <h1 class="startup-title error-text">Startup Failed</h1>
      <p class="startup-subtitle">The backend could not be started</p>
      {#if backendProcessStore.errorMessage}
        <div class="error-details">
          <code>{backendProcessStore.errorMessage}</code>
        </div>
      {/if}
      <p class="startup-hint">
        Check that the source checkout backend starts with python run.py api, then try restarting the app.
      </p>
    {:else}
      <h1 class="startup-title">Connecting{dots}</h1>
      <p class="startup-subtitle">Waiting for backend</p>
    {/if}
  </div>
</div>

<style>
  .startup-overlay {
    position: fixed;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-primary, #0a0a0f);
    z-index: 9999;
  }

  .startup-content {
    text-align: center;
    max-width: 400px;
    padding: 2rem;
  }

  .logo-container {
    margin-bottom: 2rem;
    display: flex;
    justify-content: center;
  }

  .spinner {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    border: 3px solid var(--border-primary, #2a2a3a);
    border-top-color: var(--accent-primary, #7c6cf0);
    animation: spin 1s linear infinite;
  }

  .spinner.error {
    border-top-color: var(--error, #ef4444);
    animation: none;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .startup-title {
    font-size: 1.5rem;
    font-weight: 600;
    color: var(--text-primary, #e0e0e8);
    margin: 0 0 0.5rem 0;
  }

  .error-text {
    color: var(--error, #ef4444);
  }

  .startup-subtitle {
    font-size: var(--font-size-sm);
    color: var(--text-secondary, #8888a0);
    margin: 0;
  }

  .error-details {
    margin-top: 1rem;
    padding: 0.75rem 1rem;
    background: var(--bg-secondary, #12121a);
    border-radius: 8px;
    border: 1px solid var(--error, #ef4444);
    text-align: left;
    max-height: 120px;
    overflow-y: auto;
  }

  .error-details code {
    font-size: var(--font-size-xs);
    color: var(--text-secondary, #8888a0);
    white-space: pre-wrap;
    word-break: break-all;
  }

  .startup-hint {
    margin-top: 1rem;
    font-size: var(--font-size-xs);
    color: var(--text-tertiary, #5a5a70);
  }
</style>
