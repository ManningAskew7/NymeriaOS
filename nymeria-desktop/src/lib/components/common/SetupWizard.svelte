<script lang="ts">
  import { onMount } from 'svelte';
  import type { AccountIdentity } from '$lib/types';
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { probeConnection } from '$lib/services/api.svelte';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';
  import Avatar from '$lib/components/account/Avatar.svelte';
  import RoleChip from '$lib/components/account/RoleChip.svelte';
  import { identityDisplayName } from '$lib/components/account/avatar';

  // Wizard state
  let currentStep = $state(1);
  let totalSteps = 4;

  // Form values (use saved or build-time defaults if available)
  let apiUrl = $state(configStore.apiUrl || import.meta.env.VITE_DEFAULT_API_URL || 'http://localhost:8000');
  let apiKey = $state(configStore.apiKey || import.meta.env.VITE_DEFAULT_API_KEY || '');

  // Status
  let testStatus = $state<'idle' | 'testing' | 'success' | 'error'>('idle');
  let testMessage = $state('');
  // Resolved identity from /me — surfaces in Step 3 so the user can confirm
  // they're signing in as the expected account before completing setup.
  let resolvedIdentity = $state<AccountIdentity | null>(null);

  onMount(() => {
    const initialApiUrl = apiUrl;
    void configStore.autoDetectBackendOrigin().then((detected) => {
      if (detected && apiUrl === initialApiUrl) {
        apiUrl = detected;
      }
    });
  });

  // Validates URL+token via probeConnection — a stateless module-level helper
  // that does NOT touch configStore. Writing apiUrl/apiKey here (as the
  // pre-fix code did) flips the root setup gate and unmounts the
  // wizard mid-await, destroying its $state and dropping the user into the
  // half-initialised app. Only handleComplete() persists to configStore.
  async function testConnection() {
    testStatus = 'testing';
    testMessage = '';
    resolvedIdentity = null;

    const result = await probeConnection(apiUrl, apiKey);
    if (!result.ok) {
      testStatus = 'error';
      testMessage = result.message;
      return;
    }

    resolvedIdentity = result.identity;
    testStatus = 'success';
    testMessage = 'Connection verified.';
  }

  function handleNext() {
    if (currentStep < totalSteps) {
      currentStep++;
    }
  }

  function handleBack() {
    if (currentStep > 1) {
      currentStep--;
    }
  }

  async function handleComplete() {
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;
    configStore.completeSetup();
    // Resolve /me so localStorage namespacing picks up the correct user_id
    // before the rest of the app starts reading threads/folders. Without
    // the await, scoped store reads can fire against the legacy unscoped
    // keys and momentarily render the previous user's data.
    const identity = await configStore.refreshIdentity();
    if (identity) {
      connectionsStore.upsertAccountCredential({
        apiUrl,
        apiKey,
        identity,
        makeActive: true,
      });
    }
  }

  function canProceed(): boolean {
    switch (currentStep) {
      case 1:
        return true; // Welcome screen
      case 2:
        return apiUrl.length > 0; // Backend URL
      case 3:
        return apiKey.length > 0 && testStatus === 'success'; // Account token + verified
      case 4:
        return true; // Complete
      default:
        return false;
    }
  }
</script>

<div class="wizard-overlay">
  <div class="wizard-container">
    <!-- Header -->
    <div class="wizard-header">
      <h1>Welcome to NymeriaOS</h1>
      <p class="subtitle">Connect this app to your NymeriaOS backend</p>
    </div>

    <!-- Progress indicator -->
    <div class="progress-bar">
      {#each Array(totalSteps) as _, i}
        <div
          class="progress-step"
          class:active={i + 1 === currentStep}
          class:completed={i + 1 < currentStep}
          aria-current={i + 1 === currentStep ? 'step' : undefined}
        >
          <div class="step-dot">{i + 1}</div>
        </div>
        {#if i < totalSteps - 1}
          <div class="progress-line" class:completed={i + 1 < currentStep}></div>
        {/if}
      {/each}
    </div>

    <!-- Step content -->
    <div class="wizard-content">
      {#if currentStep === 1}
        <!-- Step 1: Welcome -->
        <div class="step">
          <h2>Client Connection</h2>
          <p>Make sure you have:</p>
          <ul class="checklist">
            <li>
              <Icon name="success" size={18} />
              <span>A connection to the company network</span>
            </li>
            <li>
              <Icon name="success" size={18} />
              <span>A backend URL and nym_... account token</span>
            </li>
          </ul>
          <div class="info-box">
            <strong>Note:</strong> If the app was pre-configured, you may already be connected.
            Check Settings if you need to change the server.
          </div>
        </div>
      {:else if currentStep === 2}
        <!-- Step 2: Backend URL -->
        <div class="step">
          <h2>Backend Connection</h2>
          <p>Enter the URL where your NymeriaOS backend is running:</p>
          <div class="field">
            <label for="api-url">API URL</label>
            <input
              id="api-url"
              type="text"
              bind:value={apiUrl}
              placeholder="http://localhost:8000"
            />
            <p class="hint">Default is http://localhost:8000 if running locally</p>
          </div>
        </div>
      {:else if currentStep === 3}
        <!-- Step 3: Account Token -->
        <div class="step">
          <h2>Account Token</h2>
          <p>Paste your personal account token. Create one with <code>python run.py users add &lt;email&gt;</code>, or use the bootstrap admin token from <code>&lt;data_dir&gt;/BOOTSTRAP_TOKEN.txt</code> on first run.</p>
          <div class="field">
            <label for="api-key">Account Token</label>
            <input
              id="api-key"
              type="password"
              bind:value={apiKey}
              placeholder="nym_..."
            />
            <p class="hint">Identifies which NymeriaOS account this install connects as</p>
          </div>

          <div class="test-section">
            <Button
              variant="secondary"
              onclick={testConnection}
              disabled={testStatus === 'testing' || !apiKey}
            >
              {testStatus === 'testing' ? 'Testing…' : 'Test Connection'}
            </Button>

            {#if testMessage}
              <div
                class="test-result"
                class:success={testStatus === 'success'}
                class:error={testStatus === 'error'}
              >
                {#if testStatus === 'success'}
                  <Icon name="success" size={16} />
                {:else if testStatus === 'error'}
                  <Icon name="error" size={16} />
                {/if}
                {testMessage}
              </div>
            {/if}

            {#if resolvedIdentity}
              <div class="identity-preview">
                <Avatar identity={resolvedIdentity} size={40} state="connected" />
                <div class="identity-meta">
                  <div class="identity-line">
                    <span>You'll be signed in as</span>
                    <strong>{identityDisplayName(resolvedIdentity)}</strong>
                    <RoleChip role={resolvedIdentity.role} size="xs" />
                  </div>
                  {#if resolvedIdentity.email && resolvedIdentity.email !== resolvedIdentity.display_name}
                    <span class="identity-email">{resolvedIdentity.email}</span>
                  {/if}
                </div>
              </div>
            {/if}
          </div>
        </div>
      {:else if currentStep === 4}
        <!-- Step 4: Complete -->
        <div class="step complete-step">
          <div class="success-icon">
            <Icon name="success" size={48} />
          </div>
          <h2>Connection Ready</h2>
          <p>This app is connected to NymeriaOS. Start a thread to begin.</p>

          <div class="tips">
            <h3>Quick Tips:</h3>
            <ul>
              <li>Send a message to start your first thread</li>
              <li>Nymeria can remember information about you</li>
              <li>Create tasks to let Nymeria work autonomously</li>
            </ul>
          </div>
        </div>
      {/if}
    </div>

    <!-- Navigation -->
    <div class="wizard-nav">
      {#if currentStep > 1 && currentStep < 4}
        <Button variant="secondary" onclick={handleBack}>
          Back
        </Button>
      {:else}
        <div></div>
      {/if}

      {#if currentStep < totalSteps}
        <Button variant="primary" onclick={handleNext} disabled={!canProceed()}>
          {currentStep === 3 ? 'Continue' : 'Next'}
        </Button>
      {:else}
        <Button variant="primary" onclick={handleComplete}>
          Start a Thread
        </Button>
      {/if}
    </div>
  </div>
</div>

<style>
  .wizard-overlay {
    position: fixed;
    inset: 0;
    background: var(--bg-base);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .wizard-container {
    background: var(--bg-elevated);
    border-radius: var(--radius-lg);
    padding: var(--spacing-xl);
    max-width: 500px;
    width: 90%;
    /* §7 — floating setup wizard: shadow alone defines elevation;
       border would be redundant chrome. Tokenized to --shadow-xl. */
    box-shadow: var(--shadow-xl);
  }

  .wizard-header {
    text-align: center;
    margin-bottom: var(--spacing-lg);
  }

  .wizard-header h1 {
    margin: 0;
    font-size: 1.75rem;
    color: var(--text-primary);
  }

  .subtitle {
    color: var(--text-secondary);
    margin: var(--spacing-xs) 0 0;
  }

  .progress-bar {
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: var(--spacing-xl);
  }

  .progress-step {
    display: flex;
    align-items: center;
  }

  .step-dot {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: var(--bg-elevated-2);
    border: 2px solid var(--border-subtle);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-secondary);
    transition: all 0.2s ease;
  }

  .progress-step.active .step-dot {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    color: var(--bg-base);
  }

  .progress-step.completed .step-dot {
    background: var(--success);
    border-color: var(--success);
    color: var(--bg-base);
  }

  .progress-line {
    width: 40px;
    height: 2px;
    background: var(--border-subtle);
    margin: 0 var(--spacing-xs);
  }

  .progress-line.completed {
    background: var(--success);
  }

  .wizard-content {
    min-height: 280px;
  }

  .step h2 {
    margin: 0 0 var(--spacing-md);
    font-size: 1.25rem;
    color: var(--text-primary);
  }

  .step p {
    color: var(--text-secondary);
    margin-bottom: var(--spacing-md);
  }

  .checklist {
    list-style: none;
    padding: 0;
    margin: var(--spacing-md) 0;
  }

  .checklist li {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) 0;
    color: var(--text-primary);
  }

  .checklist li :global(svg) {
    color: var(--success);
    flex-shrink: 0;
  }

  .info-box {
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
    padding: var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .field {
    margin-bottom: var(--spacing-md);
  }

  .field label {
    display: block;
    margin-bottom: var(--spacing-xs);
    font-weight: 500;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .field input {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
  }

  .field input:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px var(--accent-tint-bg);
  }

  .hint {
    margin: var(--spacing-xs) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .test-section {
    margin-top: var(--spacing-lg);
    padding-top: var(--spacing-lg);
    border-top: 1px solid var(--border-subtle);
  }

  .test-result {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .test-result.success {
    background: rgba(var(--success-rgb), 0.15);
    color: var(--success);
  }

  .test-result.error {
    background: rgba(var(--error-rgb), 0.15);
    color: var(--error);
  }

  .identity-preview {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-md);
    box-shadow: 0 0 0 3px var(--accent-tint-bg);
  }

  .identity-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .identity-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .identity-line strong {
    color: var(--text-primary);
  }

  .identity-email {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .complete-step {
    text-align: center;
    padding: var(--spacing-lg) 0;
  }

  .success-icon {
    margin-bottom: var(--spacing-md);
  }

  .success-icon :global(svg) {
    color: var(--success);
  }

  .tips {
    text-align: left;
    margin-top: var(--spacing-xl);
    padding: var(--spacing-md);
    background: var(--bg-elevated-2);
    border-radius: var(--radius-md);
  }

  .tips h3 {
    margin: 0 0 var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
  }

  .tips ul {
    margin: 0;
    padding-left: var(--spacing-lg);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .tips li {
    margin-bottom: var(--spacing-xs);
  }

  .wizard-nav {
    display: flex;
    justify-content: space-between;
    margin-top: var(--spacing-xl);
    padding-top: var(--spacing-lg);
    border-top: 1px solid var(--border-subtle);
  }
</style>
