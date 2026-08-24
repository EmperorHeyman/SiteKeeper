<script>
  // Vault protection and the master password. The dangerous half of the app, so
  // every switch says plainly what it costs before it asks for confirmation.
  import { vaultApi } from '../lib/api.js'

  let { status, onChanged, onClose } = $props()

  const PASSWORD = 'password'
  const WINDOWS = 'windows'

  // Seeded from the effect below rather than the initialiser, so the radio
  // follows the real vault state if it changes while this is open.
  let mode = $state('')

  $effect(() => {
    mode = status.protection
  })
  let currentPassword = $state('')
  let newPassword = $state('')
  let confirmPassword = $state('')
  let busy = $state(false)
  let error = $state('')
  let done = $state('')

  const switchingOff = $derived(mode === WINDOWS && status.protection === PASSWORD)
  const switchingOn = $derived(mode === PASSWORD && status.protection === WINDOWS)
  const hasPassword = $derived(status.protection === PASSWORD)

  function reset() {
    currentPassword = ''
    newPassword = ''
    confirmPassword = ''
  }

  async function run(work, success) {
    error = ''
    done = ''
    busy = true
    try {
      const next = await work()
      onChanged?.(next)
      done = success
      reset()
    } catch (caught) {
      error = caught.detail || caught.message
    } finally {
      busy = false
    }
  }

  function applyProtection() {
    if (switchingOff) {
      const warning =
        'Your saved connections stay encrypted, but the key will be sealed to ' +
        'this Windows account instead of a password - anyone who can log in as ' +
        'you will be able to open them.\n\nContinue?'
      if (!window.confirm(warning)) {
        mode = status.protection
        return
      }
      run(
        () => vaultApi.setProtection({ mode: WINDOWS, current_password: currentPassword }),
        'Password protection is off. You will not be asked again on this account.',
      )
      return
    }
    if (switchingOn) {
      if (newPassword.length < 6) {
        error = 'Use at least 6 characters.'
        return
      }
      if (newPassword !== confirmPassword) {
        error = 'The passwords do not match.'
        return
      }
      run(
        () => vaultApi.setProtection({ mode: PASSWORD, new_password: newPassword }),
        'Your vault is now protected by a master password.',
      )
    }
  }

  function changePassword() {
    if (newPassword.length < 6) {
      error = 'Use at least 6 characters.'
      return
    }
    if (newPassword !== confirmPassword) {
      error = 'The new passwords do not match.'
      return
    }
    run(
      () => vaultApi.changePassword(currentPassword, newPassword),
      'Your master password has been updated.',
    )
  }
</script>

<div class="backdrop" role="presentation" onclick={onClose}></div>

<div class="dialog card" role="dialog" aria-modal="true">
  <header>
    <h2>Settings</h2>
    <div class="grow"></div>
    <button class="btn icon" onclick={onClose} aria-label="Close">×</button>
  </header>

  <section>
    <h3>Vault protection</h3>
    <label class="choice">
      <input type="radio" bind:group={mode} value={PASSWORD} />
      <span>
        <strong>Master password</strong>
        <em>
          Strongest option: the encryption key cannot be recovered from the files
          alone.
        </em>
      </span>
    </label>

    <label class="choice" class:disabled={!status.windows_protection_available}>
      <input
        type="radio"
        bind:group={mode}
        value={WINDOWS}
        disabled={!status.windows_protection_available}
      />
      <span>
        <strong>No password — tied to this Windows account</strong>
        <em>
          No prompt, ever. Your connections stay encrypted, but the key is sealed
          to this Windows user, so anyone who can run code as you can open them.
          Copies of the files are useless on another account or machine.
        </em>
      </span>
    </label>

    {#if switchingOff}
      <label class="field">
        Confirm your current master password
        <input type="password" bind:value={currentPassword} autocomplete="off" />
      </label>
      <button class="btn primary" disabled={busy} onclick={applyProtection}>
        Turn password protection off
      </button>
    {:else if switchingOn}
      <label class="field">
        New master password
        <input type="password" bind:value={newPassword} autocomplete="new-password" />
      </label>
      <label class="field">
        Confirm
        <input type="password" bind:value={confirmPassword} autocomplete="new-password" />
      </label>
      <button class="btn primary" disabled={busy} onclick={applyProtection}>
        Turn password protection on
      </button>
    {/if}
  </section>

  {#if hasPassword && !switchingOff && !switchingOn}
    <section>
      <h3>Change master password</h3>
      <label class="field">
        Current
        <input type="password" bind:value={currentPassword} autocomplete="off" />
      </label>
      <label class="field">
        New
        <input type="password" bind:value={newPassword} autocomplete="new-password" />
      </label>
      <label class="field">
        Confirm new
        <input type="password" bind:value={confirmPassword} autocomplete="new-password" />
      </label>
      <button class="btn" disabled={busy} onclick={changePassword}>Change password</button>
    </section>
  {/if}

  {#if error}<p class="error">{error}</p>{/if}
  {#if done}<p class="ok">{done}</p>{/if}
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    z-index: 10;
  }

  .dialog {
    position: fixed;
    z-index: 11;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: min(560px, calc(100vw - 40px));
    max-height: calc(100vh - 60px);
    overflow-y: auto;
    padding: 18px 20px;
    background: var(--panel);
  }

  header {
    display: flex;
    align-items: center;
    margin-bottom: 6px;
  }

  h2 {
    margin: 0;
    font-size: 15px;
  }

  h3 {
    margin: 0 0 10px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-faint);
  }

  section {
    padding: 14px 0;
    border-top: 1px solid var(--border-soft);
  }

  .choice {
    display: flex;
    gap: 10px;
    padding: 9px 10px;
    border-radius: var(--radius-sm);
    background: var(--card);
    margin-bottom: 8px;
    cursor: pointer;
  }

  .choice.disabled {
    opacity: 0.5;
    cursor: default;
  }

  .choice input {
    width: auto;
    margin-top: 2px;
  }

  .choice strong {
    display: block;
    font-weight: 600;
    font-size: 12.5px;
  }

  .choice em {
    display: block;
    margin-top: 3px;
    font-style: normal;
    font-size: 11.5px;
    color: var(--text-faint);
    line-height: 1.45;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-bottom: 8px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-faint);
  }

  .field input {
    text-transform: none;
    letter-spacing: normal;
    font-size: 13px;
  }

  .error {
    color: #ff8b88;
    font-size: 12px;
  }

  .ok {
    color: var(--green);
    font-size: 12px;
  }
</style>
