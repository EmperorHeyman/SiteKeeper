<script>
  // Add/edit a connection. Which fields show depends on the chosen type, the
  // same rule the Qt dialog used.
  import { KIND_LABEL } from '../lib/format.js'

  let { profile = null, defaultPorts = {}, onSave, onCancel } = $props()

  const KINDS = ['phpmyadmin', 'mysql', 'sftp', 'ftp', 'ftps']

  const BLANK = {
    label: '',
    kind: 'phpmyadmin',
    url: '',
    username: '',
    password: '',
    auth_type: 'auto',
    group: '',
    environment: 'none',
    startup_script: '',
    host: '',
    port: 0,
    database: '',
    remote_dir: '',
    local_dir: '',
    private_key_path: '',
    passive: true,
  }

  let form = $state({ ...BLANK })
  let error = $state('')

  // Re-seed when a different profile is passed in, rather than capturing only
  // whatever was set on the first render.
  $effect(() => {
    form = { ...BLANK, ...(profile ?? {}) }
  })

  const isWeb = $derived(form.kind === 'phpmyadmin')
  const isMysql = $derived(form.kind === 'mysql')
  const isSftp = $derived(form.kind === 'sftp')
  const isFtp = $derived(form.kind === 'ftp' || form.kind === 'ftps')
  const isTransfer = $derived(isSftp || isFtp)
  const portHint = $derived(defaultPorts[form.kind] ? `default ${defaultPorts[form.kind]}` : '')

  function save() {
    error = ''
    if (!form.label.trim()) {
      error = 'Please enter a display name.'
      return
    }
    if (isWeb) {
      if (!/^https?:\/\//.test(form.url.trim())) {
        error = 'URL must start with http:// or https://'
        return
      }
    } else if (!form.host.trim()) {
      error = 'Please enter the server host name or address.'
      return
    }
    onSave?.({ ...form, port: Number(form.port) || 0 })
  }
</script>

<div class="backdrop" role="presentation" onclick={onCancel}></div>

<div class="dialog card" role="dialog" aria-modal="true">
  <h2>{profile ? 'Edit connection' : 'Add connection'}</h2>

  <div class="grid">
    <label>Type
      <select bind:value={form.kind}>
        {#each KINDS as kind}<option value={kind}>{KIND_LABEL[kind]}</option>{/each}
      </select>
    </label>

    <label>Display name
      <input bind:value={form.label} placeholder="Production DB" />
    </label>

    <label>Group
      <input bind:value={form.group} placeholder="optional" />
    </label>

    <label>Environment
      <select bind:value={form.environment}>
        <option value="none">None</option>
        <option value="dev">Development</option>
        <option value="staging">Staging</option>
        <option value="prod">Production</option>
      </select>
    </label>

    {#if isWeb}
      <label class="wide">URL
        <input bind:value={form.url} placeholder="https://example.com/phpmyadmin/" />
      </label>
      <label class="wide">Authentication
        <select bind:value={form.auth_type}>
          <option value="auto">Auto-detect</option>
          <option value="cookie">phpMyAdmin login form</option>
          <option value="basic">HTTP Basic Auth (opens in your browser)</option>
        </select>
      </label>
    {:else}
      <label>Host
        <input bind:value={form.host} placeholder="db.example.com" />
      </label>
      <label>Port
        <input type="number" min="0" max="65535" bind:value={form.port} placeholder={portHint} />
      </label>
    {/if}

    {#if isMysql}
      <label class="wide">Database
        <input bind:value={form.database} placeholder="optional starting database" />
      </label>
    {/if}

    {#if isSftp}
      <label class="wide">Private key
        <input bind:value={form.private_key_path} placeholder="optional OpenSSH key path" />
      </label>
    {/if}

    <label>Username
      <input bind:value={form.username} autocomplete="off" />
    </label>

    <label>
      {isSftp && form.private_key_path ? 'Key passphrase' : 'Password'}
      <input type="password" bind:value={form.password} autocomplete="new-password" />
    </label>

    {#if isFtp}
      <label class="check wide">
        <input type="checkbox" bind:checked={form.passive} />
        <span>Passive mode (recommended)</span>
      </label>
    {/if}

    {#if isTransfer}
      <label>Remote directory
        <input bind:value={form.remote_dir} placeholder="server default" />
      </label>
      <label>Local directory
        <input bind:value={form.local_dir} placeholder="your home folder" />
      </label>
    {/if}

    {#if isWeb || isMysql}
      <label class="wide">Startup SQL
        <textarea rows="3" bind:value={form.startup_script} placeholder="SET NAMES utf8;"
        ></textarea>
      </label>
    {/if}
  </div>

  {#if error}<p class="error">{error}</p>{/if}

  <footer>
    <div class="grow"></div>
    <button class="btn" onclick={onCancel}>Cancel</button>
    <button class="btn primary" onclick={save}>Save</button>
  </footer>
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
    width: min(620px, calc(100vw - 40px));
    max-height: calc(100vh - 60px);
    overflow-y: auto;
    padding: 18px 20px;
    background: var(--panel);
  }

  h2 {
    margin: 0 0 14px;
    font-size: 15px;
  }

  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 12px;
  }

  label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-faint);
  }

  label :global(input),
  label :global(select),
  label :global(textarea) {
    text-transform: none;
    letter-spacing: normal;
    font-size: 13px;
  }

  .wide {
    grid-column: 1 / -1;
  }

  .check {
    flex-direction: row;
    align-items: center;
    gap: 8px;
    text-transform: none;
    color: var(--text-dim);
    font-size: 12px;
  }

  .check :global(input) {
    width: auto;
  }

  .error {
    color: #ff8b88;
    margin: 12px 0 0;
    font-size: 12px;
  }

  footer {
    display: flex;
    gap: 8px;
    margin-top: 16px;
  }
</style>
