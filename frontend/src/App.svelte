<script>
  // Window shell: icon rail, connection sidebar, and one or two tab panes.
  import FileManager from './components/FileManager.svelte'
  import Gate from './components/Gate.svelte'
  import Rail from './components/Rail.svelte'
  import ServerForm from './components/ServerForm.svelte'
  import Settings from './components/Settings.svelte'
  import Sidebar from './components/Sidebar.svelte'
  import SqlConsole from './components/SqlConsole.svelte'
  import { serversApi, vaultApi, waitForBackend } from './lib/api.js'
  import {
    closeTab,
    focusTab,
    moveActiveTabAcross,
    openProfile,
    paneTabs,
    setSplitView,
    tabs,
  } from './lib/tabs.svelte.js'

  let booting = $state(true)
  let bootError = $state('')
  let vault = $state({ initialized: false, unlocked: false })
  let groups = $state([])
  let defaultPorts = $state({})
  let selected = $state(null)
  let sidebarCollapsed = $state(false)
  let editing = $state(null) // null | 'new' | profile object
  let showSettings = $state(false)
  let toast = $state('')

  function notify(message) {
    toast = message
    setTimeout(() => (toast = ''), 4000)
  }

  // ----- boot -----
  $effect(() => {
    void (async () => {
      try {
        await waitForBackend()
        vault = await vaultApi.autoUnlock()
        if (vault.unlocked) await refresh()
      } catch (error) {
        bootError = error.detail || error.message
      } finally {
        booting = false
      }
    })()
  })

  async function refresh() {
    const payload = await serversApi.list()
    groups = payload.groups
    defaultPorts = payload.default_ports
  }

  async function onVaultOpened(status) {
    vault = status
    if (status.unlocked) await refresh()
  }

  // ----- connection actions -----
  async function open(server) {
    if (server.kind === 'phpmyadmin') {
      // Until the shell hosts isolated webviews, phpMyAdmin opens outside.
      try {
        const { openUrl } = await import('@tauri-apps/plugin-opener')
        await openUrl(server.url)
        notify(`Opened ${server.label} in your browser`)
      } catch {
        window.open(server.url, '_blank')
      }
      return
    }
    const tab = await openProfile(server)
    if (tab.status === 'failed') notify(tab.error)
  }

  async function saveServer(form) {
    try {
      if (editing && editing !== 'new') {
        await serversApi.update(editing.id, form)
      } else {
        await serversApi.create(form)
      }
      editing = null
      await refresh()
    } catch (error) {
      notify(error.detail || error.message)
    }
  }

  async function editSelected() {
    if (!selected) return
    try {
      editing = await serversApi.read(selected.id)
    } catch (error) {
      notify(error.detail || error.message)
    }
  }

  async function deleteSelected() {
    if (!selected) return
    if (!window.confirm(`Delete '${selected.label}'? This cannot be undone.`)) return
    try {
      await serversApi.remove(selected.id)
      selected = null
      await refresh()
    } catch (error) {
      notify(error.detail || error.message)
    }
  }

  async function lock() {
    try {
      for (const tab of [...tabs.items]) await closeTab(tab.id)
      vault = await vaultApi.lock()
    } catch (error) {
      notify(error.detail || error.message)
    }
  }

  // ----- shortcuts -----
  function onKeydown(event) {
    if (!event.ctrlKey) return
    const key = event.key.toLowerCase()
    if (event.key === ',') {
      event.preventDefault()
      showSettings = true
    } else if (key === 'b' && !event.altKey) {
      event.preventDefault()
      sidebarCollapsed = !sidebarCollapsed
    } else if (key === 's' && event.altKey) {
      event.preventDefault()
      setSplitView(!tabs.splitView)
    } else if (key === 'm' && event.altKey) {
      event.preventDefault()
      moveActiveTabAcross()
    } else if (key === 'w') {
      event.preventDefault()
      const active = tabs.active[tabs.focusedPane]
      if (active) closeTab(active)
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

{#if booting}
  <div class="center muted">Starting…</div>
{:else if bootError}
  <div class="center">
    <div class="card pad">
      <h2>The backend did not start</h2>
      <p class="muted">{bootError}</p>
    </div>
  </div>
{:else if !vault.unlocked}
  <Gate status={vault} onOpened={onVaultOpened} />
{:else}
  <div class="shell">
    <Rail
      {sidebarCollapsed}
      splitView={tabs.splitView}
      onToggleSidebar={() => (sidebarCollapsed = !sidebarCollapsed)}
      onToggleSplit={() => setSplitView(!tabs.splitView)}
      onSettings={() => (showSettings = true)}
      onLock={lock}
      onAdd={() => (editing = 'new')}
    />

    {#if !sidebarCollapsed}
      <Sidebar
        {groups}
        selectedId={selected?.id ?? null}
        onSelect={(server) => (selected = server)}
        onOpen={open}
        onAdd={() => (editing = 'new')}
        onEdit={editSelected}
        onDelete={deleteSelected}
        onCollapse={() => (sidebarCollapsed = true)}
      />
    {/if}

    <main class="panes">
      {#each tabs.splitView ? [0, 1] : [0] as pane (pane)}
        {@const paneList = paneTabs(pane)}
        {@const activeId = tabs.active[pane]}
        <section
          class="pane surface"
          class:focused={tabs.splitView && tabs.focusedPane === pane}
          onfocusin={() => (tabs.focusedPane = pane)}
          onclick={() => (tabs.focusedPane = pane)}
          role="presentation"
        >
          <div class="tabbar">
            {#each paneList as tab (tab.id)}
              <button
                class="tab"
                class:active={tab.id === activeId}
                onclick={() => focusTab(pane, tab.id)}
              >
                {#if tab.profile.environment === 'prod'}<span class="prod">●</span>{/if}
                <span class="truncate">{tab.title}</span>
                <span
                  class="x"
                  role="button"
                  tabindex="-1"
                  onclick={(event) => {
                    event.stopPropagation()
                    closeTab(tab.id)
                  }}
                  onkeydown={() => {}}>×</span
                >
              </button>
            {/each}
            {#if !paneList.length}
              <span class="faint hint">
                {pane === 0
                  ? 'Double-click a connection to open it'
                  : 'Ctrl+Alt+M moves a tab here'}
              </span>
            {/if}
          </div>

          <div class="body">
            {#each paneList as tab (tab.id)}
              {#if tab.id === activeId}
                {#if tab.kind === 'sql'}
                  <SqlConsole {tab} />
                {:else if tab.kind === 'transfer'}
                  <FileManager {tab} />
                {:else}
                  <div class="center muted">{tab.profile.label} opened in your browser.</div>
                {/if}
              {/if}
            {/each}
            {#if !paneList.length}
              <div class="center faint">No session in this pane</div>
            {/if}
          </div>
        </section>
      {/each}
    </main>
  </div>

  {#if showSettings}
    <Settings
      status={vault}
      onChanged={(next) => (vault = { ...vault, ...next })}
      onClose={() => (showSettings = false)}
    />
  {/if}

  {#if editing}
    <ServerForm
      profile={editing === 'new' ? null : editing}
      {defaultPorts}
      onSave={saveServer}
      onCancel={() => (editing = null)}
    />
  {/if}

  {#if toast}
    <div class="toast card">{toast}</div>
  {/if}
{/if}

<style>
  .center {
    display: grid;
    place-items: center;
    height: 100%;
    padding: 20px;
  }

  .pad {
    padding: 20px 24px;
  }

  .panes {
    flex: 1;
    display: flex;
    gap: 8px;
    min-width: 0;
  }

  .pane {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .pane.focused {
    border-color: var(--accent);
  }

  .tabbar {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 8px 8px 0;
    overflow-x: auto;
    min-height: 40px;
  }

  .tab {
    display: flex;
    align-items: center;
    gap: 6px;
    max-width: 220px;
    padding: 6px 10px;
    border-radius: 9px 9px 0 0;
    background: var(--panel-2);
    color: var(--text-dim);
    font-size: 12px;
    border: 1px solid transparent;
    border-bottom: 0;
  }

  .tab.active {
    background: var(--card);
    color: var(--text);
    border-color: var(--border-soft);
  }

  .prod {
    color: var(--red);
    font-size: 9px;
  }

  .x {
    color: var(--text-faint);
    padding: 0 2px;
  }

  .x:hover {
    color: var(--text);
  }

  .hint {
    font-size: 12px;
    padding: 0 6px;
  }

  .body {
    flex: 1;
    min-height: 0;
    border-top: 1px solid var(--border-soft);
  }

  .toast {
    position: fixed;
    bottom: 18px;
    left: 50%;
    transform: translateX(-50%);
    padding: 10px 16px;
    background: var(--panel);
    font-size: 12.5px;
    z-index: 20;
  }
</style>
