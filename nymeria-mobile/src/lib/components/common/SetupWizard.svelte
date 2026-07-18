<script lang="ts">
  import type { AccountIdentity } from '$lib/types';
  import { configStore } from '$lib/stores/config.svelte';
  import { probeConnection } from '$lib/services/api.svelte';
  import { exchangePastedToken } from '$lib/utils/tokenHandoff';
  import Icon from './Icon.svelte';
  import Button from './Button.svelte';
  import Spinner from './Spinner.svelte';
  import Avatar from '$lib/components/account/Avatar.svelte';
  import RoleChip from '$lib/components/account/RoleChip.svelte';
  import { identityDisplayName } from '$lib/components/account/avatar';

  let step = $state(0);
  let apiUrl = $state('');
  let apiKey = $state('');
  let testing = $state(false);
  let testResult = $state<'success' | 'error' | null>(null);
  let testMessage = $state('');
  // Resolved identity from /me — surfaces in the test step so the user can
  // confirm they're signing in as the expected account before completing.
  let resolvedIdentity = $state<AccountIdentity | null>(null);

  // Android enforces TLS for non-loopback backends (see network_security_config
  // .xml): the bearer token would otherwise be exposed in cleartext. Surface a
  // clear message instead of a confusing connection failure.
  function cleartextError(url: string): string | null {
    const trimmed = url.trim();
    if (!trimmed) return null;
    let parsed: URL;
    try {
      parsed = new URL(trimmed);
    } catch {
      return null; // let probeConnection surface a clearer parse error
    }
    if (parsed.protocol !== 'http:') return null;
    const loopback = ['localhost', '127.0.0.1', '10.0.2.2', '10.0.3.2'];
    if (loopback.includes(parsed.hostname)) return null;
    return 'Use an https:// URL. Cleartext HTTP to a non-local backend is blocked because your access token would be exposed on the network.';
  }

  function canProceed(): boolean {
    switch (step) {
      case 0: return true;
      case 1: return apiUrl.trim().length > 0;
      case 2: return testResult === 'success';
      default: return true;
    }
  }

  function next() {
    if (canProceed() && step < 3) step += 1;
  }

  function back() {
    if (step > 0) step -= 1;
  }

  // Validates URL+token via probeConnection — a stateless module-level helper
  // that does NOT touch configStore. Writing apiUrl/apiKey here (as the
  // pre-fix code did) flipped the root setup gate and unmounted
  // the wizard mid-await, dropping the user into the half-initialised app.
  // Only completeSetup() persists to configStore.
  async function testConnection() {
    testing = true;
    testResult = null;
    testMessage = '';
    resolvedIdentity = null;

    try {
      const tlsError = cleartextError(apiUrl);
      if (tlsError) {
        testResult = 'error';
        testMessage = tlsError;
        return;
      }
      const result = await probeConnection(apiUrl, apiKey);
      if (!result.ok) {
        testResult = 'error';
        testMessage = result.message;
        return;
      }

      resolvedIdentity = result.identity;
      testResult = 'success';
      testMessage = 'Connection verified.';
    } finally {
      testing = false;
    }
  }

  // True while completeSetup's token exchange runs (re-entry guard: the
  // exchange await opens a window where a second tap would mint a duplicate
  // token and re-run completion).
  let completing = $state(false);

  async function completeSetup() {
    if (completing) return;
    completing = true;
    // Paste-path twin of the #token fragment handoff's exchange: the common
    // first-run paste is the 24h bootstrap admin token, which would otherwise
    // die silently a day after install. Upgrade it to a long-lived personal
    // token before storing; any failure (including the exchange's 10s
    // timeout) keeps the pasted (probe-validated) token, so completion never
    // blocks on this.
    const cleanUrl = apiUrl.trim().replace(/\/$/, '');
    const adoptedKey = await exchangePastedToken(cleanUrl, apiKey.trim(), 'mobile-signin');
    configStore.apiUrl = cleanUrl;
    configStore.apiKey = adoptedKey;
    configStore.completeSetup();
    // Resolve /me so localStorage namespacing picks up the correct user_id
    // before the rest of the app starts reading threads/folders. Without
    // the await, scoped store reads can fire against legacy unscoped keys
    // and momentarily render the previous user's data.
    await configStore.refreshIdentity();
  }
</script>

<div class="setup-wizard">
  <!-- Progress dots -->
  <div class="progress">
    {#each [0, 1, 2, 3] as i}
      <div class="progress-dot" class:active={i === step} class:completed={i < step}></div>
      {#if i < 3}
        <div class="progress-line" class:completed={i < step}></div>
      {/if}
    {/each}
  </div>

  <!-- Step content -->
  <div class="step-content">
    {#if step === 0}
      <div class="step">
        <div class="step-icon">
          <Icon name="chat" size={40} />
        </div>
        <h2>Welcome to Nymeria</h2>
        <p>Let's connect your mobile app to your Nymeria backend.</p>
        <div class="checklist">
          <div class="check-item">
            <Icon name="check" size={18} />
            <span>Nymeria backend running on your network</span>
          </div>
          <div class="check-item">
            <Icon name="check" size={18} />
            <span>A <code>nym_...</code> account token for this device</span>
          </div>
          <div class="check-item">
            <Icon name="check" size={18} />
            <span>Phone connected to same WiFi</span>
          </div>
        </div>
      </div>

    {:else if step === 1}
      <div class="step">
        <h2>Backend URL</h2>
        <p>Enter the network address of your Nymeria backend. Find it by checking your computer's local IP address.</p>
        <div class="input-group">
          <label for="api-url">API URL</label>
          <input
            id="api-url"
            type="url"
            bind:value={apiUrl}
            placeholder="https://nymeria.example.com"
          />
          <span class="input-hint">Use https:// for a remote backend. Plain http:// works only for a local backend (localhost / emulator).</span>
        </div>
      </div>

    {:else if step === 2}
      <div class="step">
        <h2>Account Token</h2>
        <p>Paste your personal account token (create one with <code>python run.py users add &lt;email&gt;</code>), or use the bootstrap admin token from <code>BOOTSTRAP_TOKEN.txt</code> on first run.</p>
        <div class="input-group">
          <label for="api-key">Account Token</label>
          <input
            id="api-key"
            type="password"
            bind:value={apiKey}
            placeholder="nym_..."
          />
        </div>

        <div class="test-section">
          <Button
            variant="secondary"
            onclick={testConnection}
            disabled={!apiUrl.trim() || !apiKey.trim() || testing}
            loading={testing}
          >
            {#if testing}
              Testing…
            {:else}
              Test Connection
            {/if}
          </Button>

          {#if testResult === 'success'}
            <div class="test-result success">
              <Icon name="success" size={16} />
              <span>{testMessage}</span>
            </div>
          {:else if testResult === 'error'}
            <div class="test-result error">
              <Icon name="error" size={16} />
              <span>{testMessage}</span>
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

    {:else if step === 3}
      <div class="step">
        <div class="step-icon success-icon">
          <Icon name="success" size={48} />
        </div>
        <h2>Connection ready</h2>
        <p>This app is connected to Nymeria.</p>
        <div class="tips">
          <h3>Quick tips:</h3>
          <ul>
            <li>Swipe left/right to navigate panels</li>
            <li>Tap Enter to send messages</li>
            <li>Shift+Enter for new lines</li>
          </ul>
        </div>
      </div>
    {/if}
  </div>

  <!-- Navigation buttons -->
  <div class="nav-buttons">
    {#if step > 0 && step < 3}
      <Button variant="ghost" onclick={back}>Back</Button>
    {:else}
      <div></div>
    {/if}

    {#if step < 3}
      <Button variant="primary" onclick={next} disabled={!canProceed()}>
        {step === 0 ? 'Get Started' : 'Next'}
      </Button>
    {:else}
      <Button variant="primary" onclick={completeSetup} disabled={completing}>
        {completing ? 'Connecting…' : 'Start a Thread'}
      </Button>
    {/if}
  </div>
</div>

<style>
  .setup-wizard {
    display: flex;
    flex-direction: column;
    height: 100dvh;
    padding: var(--spacing-lg);
    padding-top: calc(var(--spacing-xl) + var(--safe-area-top));
    background: var(--bg-base);
  }

  .progress {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    margin-bottom: var(--spacing-xl);
  }

  .progress-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--border-default);
    transition: all var(--transition-fast);
  }

  .progress-dot.active {
    background: var(--accent-primary);
    transform: scale(1.2);
  }

  .progress-dot.completed {
    background: var(--accent-primary);
  }

  .progress-line {
    width: 40px;
    height: 2px;
    background: var(--border-default);
    transition: background var(--transition-fast);
  }

  .progress-line.completed {
    background: var(--accent-primary);
  }

  .step-content {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow-y: auto;
  }

  .step {
    width: 100%;
    max-width: 400px;
    text-align: center;
  }

  .step-icon {
    color: var(--accent-primary);
    margin-bottom: var(--spacing-lg);
  }

  .success-icon {
    color: var(--success);
  }

  .step h2 {
    font-size: var(--font-size-xl);
    font-weight: 700;
    margin-bottom: var(--spacing-sm);
  }

  .step p {
    color: var(--text-secondary);
    margin-bottom: var(--spacing-lg);
  }

  .checklist {
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .check-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    color: var(--text-secondary);
  }

  .check-item :global(.icon) {
    color: var(--accent-primary);
  }

  .input-group {
    text-align: left;
    margin-bottom: var(--spacing-md);
  }

  .input-group label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-secondary);
    margin-bottom: var(--spacing-xs);
  }

  .input-group input {
    width: 100%;
    font-size: 16px;
    padding: var(--spacing-md);
    min-height: var(--touch-target-min);
  }

  .input-hint {
    display: block;
    margin-top: var(--spacing-xs);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .test-section {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-md);
    margin-top: var(--spacing-md);
  }

  .test-result {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    width: 100%;
    text-align: left;
  }

  .test-result.success {
    background: rgba(var(--success-rgb), 0.1);
    color: var(--success);
  }

  .test-result.error {
    background: rgba(248, 113, 113, 0.1);
    color: var(--error);
  }

  .identity-preview {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    width: 100%;
    background: var(--bg-elevated);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-md);
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
    font-size: 12px;
    color: var(--text-muted);
  }

  .tips {
    text-align: left;
    margin-top: var(--spacing-md);
  }

  .tips h3 {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: var(--spacing-sm);
  }

  .tips ul {
    list-style: none;
    padding: 0;
  }

  .tips li {
    padding: var(--spacing-xs) 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .tips li::before {
    content: '→ ';
    color: var(--accent-primary);
  }

  .nav-buttons {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }
</style>
