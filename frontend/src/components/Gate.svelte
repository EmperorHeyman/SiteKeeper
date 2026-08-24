<script>
  // First-run and unlock screens. Shown until the vault is open, because
  // nothing else in the app works without it.
  import { vaultApi } from '../lib/api.js'

  let { status, onOpened } = $props()

  let password = $state('')
  let confirm = $state('')
  let skipPassword = $state(false)
  let error = $state('')
  let working = $state(false)

  const firstRun = $derived(!status.initialized)

  async function submit() {
    error = ''
    working = true
    try {
      if (firstRun) {
        if (!skipPassword) {
          if (password.length < 6) throw new Error('Use at least 6 characters.')
          if (password !== confirm) throw new Error('The passwords do not match.')
        }
        onOpened?.(await vaultApi.create(skipPassword ? '' : password))
      } else {
        onOpened?.(await vaultApi.unlock(password))
      }
    } catch (caught) {
      error = caught.detail || caught.message
    } finally {
      working = false
      password = ''
      confirm = ''
    }
  }
</script>

<div class="gate">
  <div class="panel card">
    <h1>Sitekeeper</h1>

    {#if firstRun}
      <p class="muted">
        Choose a master password. It protects the encryption key for your stored
        credentials.
      </p>

      {#if !skipPassword}
        <input
          type="password"
          placeholder="Master password"
          bind:value={password}
          onkeydown={(event) => event.key === 'Enter' && submit()}
        />
        <input
          type="password"
          placeholder="Confirm"
          bind:value={confirm}
          onkeydown={(event) => event.key === 'Enter' && submit()}
        />
      {/if}

      {#if status.windows_protection_available}
        <label class="check">
          <input type="checkbox" bind:checked={skipPassword} />
          <span>Don't use a master password</span>
        </label>
        {#if skipPassword}
          <p class="faint small">
            Your connections still get encrypted, but the key is sealed to this
            Windows account instead of a password. You can change this later in
            Settings.
          </p>
        {/if}
      {/if}
    {:else}
      <p class="muted">Enter your master password to unlock your saved connections.</p>
      <input
        type="password"
        placeholder="Master password"
        bind:value={password}
        onkeydown={(event) => event.key === 'Enter' && submit()}
      />
    {/if}

    {#if error}<p class="error">{error}</p>{/if}

    <button class="btn primary" disabled={working} onclick={submit}>
      {firstRun ? 'Create vault' : 'Unlock'}
    </button>
  </div>
</div>

<style>
  .gate {
    display: grid;
    place-items: center;
    height: 100%;
  }

  .panel {
    width: 360px;
    padding: 26px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    background: var(--panel);
  }

  h1 {
    margin: 0;
    font-size: 17px;
  }

  p {
    margin: 0;
    font-size: 12.5px;
    line-height: 1.5;
  }

  .small {
    font-size: 11.5px;
  }

  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    color: var(--text-dim);
  }

  .check input {
    width: auto;
  }

  .error {
    color: #ff8b88;
  }
</style>
