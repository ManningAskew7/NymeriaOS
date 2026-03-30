<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';

  // Wizard state
  let currentStep = $state(1);
  let totalSteps = 4;

  // Form values (use build-time defaults if available)
  let apiUrl = $state(import.meta.env.VITE_DEFAULT_API_URL || 'http://localhost:8000');
  let apiKey = $state(import.meta.env.VITE_DEFAULT_API_KEY || '');

  // Status
  let testStatus = $state<'idle' | 'testing' | 'success' | 'error'>('idle');
  let testMessage = $state('');
  let backendInfo = $state<{ version?: string; provider?: string } | null>(null);

  async function testConnection() {
    testStatus = 'testing';
    testMessage = '';
    backendInfo = null;

    // Temporarily set config for the test
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;

    try {
      const isHealthy = await api.healthCheck();
      if (isHealthy) {
        testStatus = 'success';
        testMessage = 'Connected successfully!';

        // Try to get server settings to confirm full access
        try {
          const settings = await api.getServerSettings();
          backendInfo = {
            provider: settings.llm_provider,
          };
        } catch {
          // Server settings not available, but basic health is OK
        }
      } else {
        testStatus = 'error';
        testMessage = 'Server returned unhealthy status';
      }
    } catch (e) {
      testStatus = 'error';
      if (e instanceof Error) {
        if (e.message.includes('401') || e.message.includes('403')) {
          testMessage = 'Invalid API key. Check that it matches your backend .env file.';
        } else if (e.message.includes('fetch') || e.message.includes('network')) {
          testMessage = 'Cannot connect to server. Is the backend running?';
        } else {
          testMessage = e.message;
        }
      } else {
        testMessage = 'Connection failed';
      }
    }
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

  function handleComplete() {
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;
    configStore.completeSetup();
  }

  function canProceed(): boolean {
    switch (currentStep) {
      case 1:
        return true; // Welcome screen
      case 2:
        return apiUrl.length > 0; // Backend URL
      case 3:
        return apiKey.length > 0 && testStatus === 'success'; // API Key + verified
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
      <h1>Welcome to Nymeria</h1>
      <p class="subtitle">Let's get you set up in just a few steps</p>
    </div>

    <!-- Progress indicator -->
    <div class="progress-bar">
      {#each Array(totalSteps) as _, i}
        <div
          class="progress-step"
          class:active={i + 1 === currentStep}
          class:completed={i + 1 < currentStep}
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
          <h2>Before You Begin</h2>
          <p>Make sure you have:</p>
          <ul class="checklist">
            <li>
              <Icon name="success" size={18} />
              <span>A connection to the company network</span>
            </li>
            <li>
              <Icon name="success" size={18} />
              <span>The backend server URL and API key from your administrator</span>
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
          <p>Enter the URL where your Nymeria backend is running:</p>
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
        <!-- Step 3: API Key -->
        <div class="step">
          <h2>API Key</h2>
          <p>Enter the API key from your backend .env file (NYMERIA_API_KEY):</p>
          <div class="field">
            <label for="api-key">API Key</label>
            <input
              id="api-key"
              type="password"
              bind:value={apiKey}
              placeholder="Enter your API key"
            />
            <p class="hint">This authenticates your desktop app with the backend</p>
          </div>

          <div class="test-section">
            <Button
              variant="secondary"
              onclick={testConnection}
              disabled={testStatus === 'testing' || !apiKey}
            >
              {testStatus === 'testing' ? 'Testing...' : 'Test Connection'}
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

            {#if backendInfo}
              <div class="backend-info">
                LLM Provider: <strong>{backendInfo.provider}</strong>
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
          <h2>You're All Set!</h2>
          <p>Nymeria is ready to help you. Start a conversation to begin.</p>

          <div class="tips">
            <h3>Quick Tips:</h3>
            <ul>
              <li>Type a message to chat with Nymeria</li>
              <li>Nymeria can remember information about you</li>
              <li>Create TODOs to let Nymeria work autonomously</li>
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
          Start Chatting
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
    border: 1px solid var(--border-subtle);
    padding: var(--spacing-xl);
    max-width: 500px;
    width: 90%;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
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
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15);
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
    background: rgba(52, 211, 153, 0.15);
    color: var(--success);
  }

  .test-result.error {
    background: rgba(248, 113, 113, 0.15);
    color: var(--error);
  }

  .backend-info {
    margin-top: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
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
