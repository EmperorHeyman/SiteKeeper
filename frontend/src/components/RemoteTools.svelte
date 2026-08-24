<script>
  // The server-side tools, in one panel: search, disk usage, a command runner,
  // the snippet library, live logs and permissions. Every one of them runs on
  // the server; none of them is offered when the connection has no shell.
  import { humanSize } from '../lib/format.js'
  import { toolsApi, transferApi } from '../lib/api.js'

  let {
    sessionId,
    remoteDir = '/',
    localDir = '',
    host = '',
    user = '',
    selection = [],
    canExec = false,
    tool = $bindable('search'),
    onNavigate,
    onStatus,
    onClose,
  } = $props()

  const TOOLS = [
    { id: 'search', label: 'Search', shell: true },
    { id: 'disk', label: 'Disk usage', shell: true },
    { id: 'run', label: 'Command', shell: true },
    { id: 'snippets', label: 'Snippets', shell: false },
    { id: 'logs', label: 'Logs', shell: true },
    { id: 'perms', label: 'Permissions', shell: false },
  ]

  // ----- search -----
  let pattern = $state('')
  let include = $state('')
  let useRegex = $state(false)
  let ignoreCase = $state(false)
  let hits = $state([])
  let searchNote = $state('')

  async function search() {
    if (!pattern.trim()) return (searchNote = 'Type something to search for.')
    searchNote = 'Searching…'
    try {
      const result = await transferApi.grep(sessionId, {
        root: remoteDir,
        pattern,
        fixed: !useRegex,
        ignore_case: ignoreCase,
        include,
      })
      hits = result.hits
      searchNote =
        result.error ||
        `${result.hits.length} match(es) via ${result.tool}${result.truncated ? ' — more were found' : ''}`
    } catch (error) {
      searchNote = error.detail || error.message
    }
  }

  // ----- disk usage -----
  let usage = $state(null)
  let usagePath = $state('')

  async function measure(path) {
    usagePath = path
    try {
      usage = await transferApi.diskUsage(sessionId, path)
    } catch (error) {
      onStatus?.(error.detail || error.message)
    }
  }

  // ----- command runner -----
  let command = $state('')
  let output = $state('')

  async function run(text = command) {
    const wanted = text.trim()
    if (!wanted) return
    output += `$ ${wanted}\n`
    command = ''
    try {
      const result = await transferApi.exec(sessionId, wanted, remoteDir)
      output += (result.stdout || '') + (result.stderr || '')
      if (!result.ok) output += `[exit status ${result.exit_status}]\n`
    } catch (error) {
      output += `[${error.detail || error.message}]\n`
    }
    output += '\n'
  }

  // ----- snippets -----
  let snippets = $state([])
  let editing = $state(null)
  let placeholders = $state([])

  async function loadSnippets() {
    const payload = await toolsApi.snippets()
    snippets = payload.snippets
    placeholders = payload.placeholders
  }

  function context() {
    return {
      remote_dir: remoteDir,
      local_dir: localDir,
      file: selection[0]?.name ?? '',
      path: selection[0] ? joinRemote(remoteDir, selection[0].name) : '',
      host,
      user,
    }
  }

  async function runSnippet(snippet) {
    const { command: rendered } = await toolsApi.renderSnippet(snippet.command, context())
    if (snippet.confirm && !window.confirm(`Run on the server:\n\n${rendered}`)) return
    tool = 'run'
    await run(rendered)
  }

  async function saveSnippet() {
    if (!editing?.name?.trim() || !editing?.command?.trim()) {
      return onStatus?.('A snippet needs a name and a command.')
    }
    await toolsApi.saveSnippet({ ...editing, tags: editing.tags ?? [] })
    editing = null
    await loadSnippets()
  }

  async function deleteSnippet(snippet) {
    if (!window.confirm(`Delete the snippet “${snippet.name}”?`)) return
    await toolsApi.deleteSnippet(snippet.id)
    await loadSnippets()
  }

  // ----- logs -----
  let logs = $state([])
  let logPath = $state('')
  let logText = $state('')
  let logFilter = $state('')
  let following = $state(false)

  async function findLogs() {
    try {
      const payload = await transferApi.logs(sessionId, remoteDir)
      logs = payload.logs
      if (!logPath && logs.length) logPath = logs[0]
    } catch (error) {
      onStatus?.(error.detail || error.message)
    }
  }

  async function pullLog() {
    if (!logPath) return
    try {
      const payload = await transferApi.tail(sessionId, logPath, 300)
      logText = payload.text
    } catch (error) {
      onStatus?.(error.detail || error.message)
      following = false
    }
  }

  const shownLog = $derived.by(() => {
    const needle = logFilter.trim().toLowerCase()
    if (!needle) return logText
    return logText
      .split('\n')
      .filter((line) => line.toLowerCase().includes(needle))
      .join('\n')
  })

  // Polling stands in for tail -f: the page asks for the last lines again every
  // couple of seconds, which is indistinguishable at human speed and needs no
  // long-lived socket.
  $effect(() => {
    if (!following || !logPath) return
    pullLog()
    const timer = setInterval(pullLog, 2000)
    return () => clearInterval(timer)
  })

  // ----- permissions -----
  let presets = $state([])
  let octal = $state('644')
  let recursive = $state(false)
  let scope = $state('all')

  async function loadPresets() {
    const payload = await transferApi.permissionPresets()
    presets = payload.presets
  }

  async function applyMode() {
    const target = selection[0]
    if (!target) return onStatus?.('Select a remote entry first.')
    const mode = parseInt(octal, 8)
    if (Number.isNaN(mode)) return onStatus?.(`${octal} is not an octal value.`)
    if ((mode & 0o002) !== 0 && !window.confirm('That is world-writable. Apply it anyway?')) return
    try {
      await transferApi.chmod(
        sessionId,
        joinRemote(remoteDir, target.name),
        mode,
        recursive,
        scope,
      )
      onStatus?.(`${target.name} is now ${octal}.`)
    } catch (error) {
      onStatus?.(error.detail || error.message)
    }
  }

  function joinRemote(base, name) {
    return (base === '/' ? '' : base.replace(/\/+$/, '')) + '/' + name
  }

  // First use of a view loads what it needs.
  $effect(() => {
    if (tool === 'disk' && usagePath !== remoteDir) measure(remoteDir)
    if (tool === 'snippets' && !snippets.length) loadSnippets()
    if (tool === 'logs' && !logs.length) findLogs()
    if (tool === 'perms' && !presets.length) loadPresets()
  })
</script>

<section class="tools">
  <header>
    {#each TOOLS as entry (entry.id)}
      <button
        class="btn"
        class:primary={tool === entry.id}
        disabled={entry.shell && !canExec}
        title={entry.shell && !canExec
          ? 'This connection has no shell, so nothing can be run on the server'
          : ''}
        onclick={() => (tool = entry.id)}>{entry.label}</button
      >
    {/each}
    <div class="grow"></div>
    <button class="btn" onclick={() => onClose?.()}>Close</button>
  </header>

  {#if tool === 'search'}
    <div class="row">
      <input class="mono grow" placeholder="Text to find" bind:value={pattern} />
      <input class="mono small" placeholder="*.php" bind:value={include} />
      <label class="faint"><input type="checkbox" bind:checked={useRegex} /> regex</label>
      <label class="faint"><input type="checkbox" bind:checked={ignoreCase} /> ignore case</label>
      <button class="btn primary" onclick={search}>Search</button>
    </div>
    <p class="faint note">{searchNote} — in {remoteDir}</p>
    <div class="scroll">
      {#each hits as hit (hit.path + hit.line)}
        <button class="hit" onclick={() => onNavigate?.(hit.path)}>
          <span class="truncate mono">{hit.path}:{hit.line}</span>
          <span class="truncate faint">{hit.text}</span>
        </button>
      {/each}
    </div>
  {:else if tool === 'disk'}
    <div class="row">
      <span class="truncate mono grow">{usage?.root ?? usagePath}</span>
      <button
        class="btn"
        onclick={() => measure((usagePath || '/').replace(/\/[^/]*$/, '') || '/')}>Up</button
      >
      <button class="btn" onclick={() => measure(usagePath)}>Refresh</button>
    </div>
    <p class="faint note">{usage ? `${humanSize(usage.total)} in total` : 'Measuring…'}</p>
    <div class="scroll">
      {#each usage?.entries ?? [] as entry (entry.path)}
        <div class="du">
          <button class="link truncate" onclick={() => measure(entry.path)}>{entry.name}</button>
          <span class="right faint">{humanSize(entry.size)}</span>
          <div class="bar"><div class="fill" style:width={`${entry.share * 100}%`}></div></div>
          <span class="faint right">{(entry.share * 100).toFixed(1)}%</span>
          <button class="btn icon" title="Show in the file list" onclick={() => onNavigate?.(entry.path)}
            >→</button
          >
        </div>
      {/each}
    </div>
  {:else if tool === 'run'}
    <div class="row">
      <input
        class="mono grow"
        placeholder="systemctl restart nginx"
        bind:value={command}
        onkeydown={(event) => {
          if (event.key === 'Enter') run()
        }}
      />
      <button class="btn primary" onclick={() => run()}>Run</button>
      <button class="btn" onclick={() => (output = '')}>Clear</button>
    </div>
    <p class="faint note">Runs in {remoteDir}</p>
    <pre class="output mono">{output}</pre>
  {:else if tool === 'snippets'}
    <div class="row">
      <button
        class="btn"
        onclick={() => (editing = { name: '', command: '', description: '', confirm: false })}
        >New</button
      >
      {#if editing}
        <input class="small" placeholder="Name" bind:value={editing.name} />
        <input class="mono grow" placeholder="Command" bind:value={editing.command} />
        <label class="faint"><input type="checkbox" bind:checked={editing.confirm} /> ask first</label>
        <button class="btn primary" onclick={saveSnippet}>Save</button>
        <button class="btn" onclick={() => (editing = null)}>Cancel</button>
      {:else}
        <span class="faint note">
          Placeholders: {placeholders.map((item) => `{${item.name}}`).join(', ')}
        </span>
      {/if}
    </div>
    <div class="scroll">
      {#each snippets as snippet (snippet.id)}
        <div class="snippet">
          <span class="truncate">{snippet.name}</span>
          <span class="truncate faint mono">{snippet.command}</span>
          <button class="btn" disabled={!canExec} onclick={() => runSnippet(snippet)}>Run</button>
          <button class="btn" onclick={() => (editing = { ...snippet })}>Edit</button>
          <button class="btn danger" onclick={() => deleteSnippet(snippet)}>✕</button>
        </div>
      {/each}
    </div>
  {:else if tool === 'logs'}
    <div class="row">
      <select bind:value={logPath} onchange={() => pullLog()}>
        {#each logs as candidate (candidate)}
          <option value={candidate}>{candidate}</option>
        {/each}
      </select>
      <input class="small" placeholder="Only lines with…" bind:value={logFilter} />
      <button class="btn" onclick={() => (following = !following)}>
        {following ? 'Stop following' : 'Follow'}
      </button>
      <button class="btn" onclick={pullLog}>Refresh</button>
      <button class="btn" onclick={findLogs}>Find logs</button>
    </div>
    <pre class="output mono">{shownLog}</pre>
  {:else if tool === 'perms'}
    <div class="row">
      <select
        onchange={(event) => {
          const found = presets.find((item) => item.label === event.currentTarget.value)
          if (found) octal = found.octal
        }}
      >
        <option value="">Choose a preset…</option>
        {#each presets as preset (preset.label)}
          <option value={preset.label} title={preset.note}
            >{preset.label}{preset.risky ? '  ⚠' : ''}</option
          >
        {/each}
      </select>
      <input class="mono tiny" bind:value={octal} maxlength="4" />
      <label class="faint"><input type="checkbox" bind:checked={recursive} /> recursively</label>
      {#if recursive}
        <select bind:value={scope}>
          <option value="all">everything</option>
          <option value="files">files only</option>
          <option value="dirs">folders only</option>
        </select>
      {/if}
      <button class="btn primary" onclick={applyMode}>Apply</button>
    </div>
    <p class="faint note">
      {selection[0]
        ? `Applies to ${selection[0].name}`
        : 'Select a remote entry to change its permissions'}
      {#if recursive && !canExec}· recursive needs a shell{/if}
    </p>
  {/if}
</section>

<style>
  .tools {
    display: flex;
    flex-direction: column;
    gap: 6px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    padding: 8px;
    max-height: 320px;
  }

  header,
  .row {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 11.5px;
  }

  .note {
    margin: 0;
    font-size: 11px;
  }

  input,
  select {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 3px 6px;
    font-size: 11.5px;
  }

  input.small,
  select {
    min-width: 120px;
  }

  input.tiny {
    width: 58px;
  }

  .scroll {
    overflow: auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .output {
    margin: 0;
    overflow: auto;
    min-height: 90px;
    max-height: 220px;
    background: var(--panel-2);
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    padding: 6px;
    font-size: 11.5px;
    white-space: pre-wrap;
  }

  .hit,
  .du,
  .snippet {
    display: grid;
    align-items: center;
    gap: 8px;
    text-align: left;
    font-size: 11.5px;
    padding: 2px 4px;
    border-radius: 4px;
    background: none;
    border: 0;
    color: inherit;
  }

  .hit {
    grid-template-columns: minmax(0, 40%) minmax(0, 1fr);
    cursor: pointer;
    font: inherit;
  }

  .du {
    grid-template-columns: minmax(0, 1fr) 80px 120px 54px 30px;
  }

  .snippet {
    grid-template-columns: minmax(0, 30%) minmax(0, 1fr) auto auto auto;
  }

  .hit:hover,
  .du:hover,
  .snippet:hover {
    background: var(--card-hover);
  }

  .link {
    background: none;
    border: 0;
    color: var(--accent);
    font: inherit;
    text-align: left;
    cursor: pointer;
    padding: 0;
  }

  .bar {
    height: 6px;
    background: var(--panel-2);
    border-radius: 999px;
    overflow: hidden;
  }

  .fill {
    height: 100%;
    background: var(--accent);
  }

  .right {
    text-align: right;
  }
</style>
