<script>
  // Dual-pane transfer view: local on the left, remote on the right, transfer
  // bar between them. Progress arrives over the backend WebSocket.
  //
  // Everything past plain copying is here too: per-pane navigation history,
  // folder sizes and dates that account for their contents, hash comparison,
  // the controllable transfer queue, mirrored navigation, undo of overwrites,
  // and - when the connection has a shell - the server-side tools.
  import { onBackendEvent, toolsApi, transferApi } from '../lib/api.js'
  import { humanSize, localPath, remotePath } from '../lib/format.js'
  import { NavHistory, mirrorPath } from '../lib/navhistory.js'
  import CompareView from './CompareView.svelte'
  import FilePane from './FilePane.svelte'
  import RemoteTools from './RemoteTools.svelte'
  import TransferQueue from './TransferQueue.svelte'

  let { tab } = $props()

  let local = $state({ path: '', entries: [], at_root: false })
  let remote = $state({ path: '/', entries: [], at_root: true })
  let localSelection = $state([])
  let remoteSelection = $state([])
  let remoteActive = $state(true)
  let busy = $state(false)
  let status = $state('')
  let progress = $state(null)

  // Queue, comparison and tool panels.
  let queueItems = $state([])
  let queueStats = $state({})
  let showQueue = $state(false)
  let report = $state(null)
  let showCompare = $state(false)
  let showTools = $state(false)
  let toolView = $state('search')
  let historyEntries = $state([])
  let showHistory = $state(false)

  // Navigation memory per pane, plus the flags the panes render.
  const localHistory = new NavHistory()
  const remoteHistory = new NavHistory()
  let localNav = $state({ back: false, forward: false, recent: [] })
  let remoteNav = $state({ back: false, forward: false, recent: [] })
  let replaying = false

  // Mirrored navigation, anchored at whichever pair of directories was paired
  // first, so it survives walking up and down either side.
  let mirror = $state(false)
  let mirrorBases = { local: '', remote: '' }
  let mirroring = false

  const activeSelection = $derived(remoteActive ? remoteSelection : localSelection)
  const canExec = $derived(Boolean(tab.canExec ?? tab.info?.can_exec))
  const isProduction = $derived(tab.profile.environment === 'prod')

  function report_(message) {
    status = message
  }

  async function guard(work) {
    busy = true
    try {
      await work()
    } catch (error) {
      report_(error.detail || error.message)
    } finally {
      busy = false
    }
  }

  function syncNav() {
    localNav = {
      back: localHistory.canGoBack,
      forward: localHistory.canGoForward,
      recent: localHistory.recent(12),
    }
    remoteNav = {
      back: remoteHistory.canGoBack,
      forward: remoteHistory.canGoForward,
      recent: remoteHistory.recent(12),
    }
  }

  // ----- loading -----
  async function loadLocal(path) {
    await guard(async () => {
      local = await transferApi.localList(path)
      localSelection = []
      if (!replaying) localHistory.visit(local.path)
      syncNav()
      if (mirror) mirrorToRemote(local.path)
    })
  }

  async function loadRemote(path) {
    if (tab.status !== 'open') return
    await guard(async () => {
      remote = await transferApi.list(tab.sessionId, path)
      remoteSelection = []
      if (!replaying) remoteHistory.visit(remote.path)
      syncNav()
      loadFolderStats(remote.path, remote.entries)
      if (mirror) mirrorToLocal(remote.path)
    })
  }

  /** Folder sizes and newest-content dates, fetched after the listing paints. */
  async function loadFolderStats(path, entries) {
    const names = entries.filter((entry) => entry.is_dir && !entry.is_link).map((e) => e.name)
    if (!names.length || names.length > 120) return
    try {
      const payload = await transferApi.folderStats(tab.sessionId, path, names)
      if (payload.parent !== remote.path) return // the user moved on
      remote = {
        ...remote,
        entries: remote.entries.map((entry) => {
          const found = payload.stats[entry.name]
          if (!found) return entry
          return { ...entry, size: found.size, modified: found.newest ?? entry.modified }
        }),
      }
    } catch {
      // Statistics are a nicety; a server that will not walk its own tree is
      // not a reason to interrupt anything.
    }
  }

  $effect(() => {
    if (tab.status !== 'open' || remote.entries.length || busy) return
    // First paint: home directory on both sides.
    guard(async () => {
      const start = tab.profile.remote_dir?.trim()
      remote = start
        ? await transferApi.list(tab.sessionId, start)
        : await transferApi.home(tab.sessionId)
      remoteHistory.visit(remote.path)
      local = await transferApi.localList(tab.profile.local_dir || '')
      localHistory.visit(local.path)
      syncNav()
      loadFolderStats(remote.path, remote.entries)
      report_(tab.banner ?? '')
    })
  })

  $effect(() => {
    if (tab.status === 'failed') report_(tab.error || 'Could not connect.')
  })

  // ----- live progress -----
  $effect(() =>
    onBackendEvent((event, payload) => {
      if (payload.session_id !== tab.sessionId) return
      if (event === 'transfer.started') {
        progress = { total: payload.total, done: 0, name: '', percent: 0 }
        report_(
          `Transferring ${payload.total} file(s)…` +
            (payload.skipped ? ` (${payload.skipped} skipped by the ignore rules)` : ''),
        )
        if (payload.total > 1) showQueue = true
        refreshQueue()
      } else if (event === 'transfer.progress') {
        const percent = payload.total
          ? Math.round((payload.transferred * 100) / payload.total)
          : 0
        progress = { ...(progress ?? { total: 1, done: 0 }), name: payload.name, percent }
      } else if (event === 'transfer.item') {
        queueItems = upsert(queueItems, payload)
      } else if (event === 'transfer.stats') {
        queueStats = payload
      } else if (event === 'transfer.file_done') {
        progress = progress ? { ...progress, done: progress.done + 1 } : progress
      } else if (event === 'transfer.error') {
        report_(`${payload.name}: ${payload.message}`)
      } else if (event === 'transfer.finished') {
        progress = null
        const parts = [`${payload.completed} file(s) transferred`]
        if (payload.failed) parts.push(`${payload.failed} failed`)
        if (payload.cancelled) parts.push('cancelled')
        report_(parts.join(', ') + '.')
        loadLocal(local.path)
        loadRemote(remote.path)
        refreshQueue()
      } else if (event === 'transfer.failed') {
        progress = null
        report_(payload.message)
      }
    }),
  )

  function upsert(items, item) {
    const index = items.findIndex((existing) => existing.id === item.id)
    if (index < 0) return [...items, item]
    const copy = [...items]
    copy[index] = item
    return copy
  }

  async function refreshQueue() {
    try {
      const payload = await transferApi.queue(tab.sessionId)
      queueItems = payload.items
      queueStats = payload.stats
    } catch {
      // The session may have closed under us; nothing to show is fine.
    }
  }

  // ----- navigation -----
  function navigateLocal(target) {
    loadLocal(target === '..' ? localPath.parent(local.path) : target)
  }

  function navigateRemote(target) {
    loadRemote(target === '..' ? remotePath.parent(remote.path) : target)
  }

  async function replay(load, target) {
    if (!target) return
    replaying = true
    try {
      await load(target)
    } finally {
      replaying = false
      syncNav()
    }
  }

  // ----- mirrored navigation -----
  function anchorMirror() {
    if (!mirrorBases.local || !mirrorBases.remote) {
      if (!local.path || !remote.path) return false
      mirrorBases = { local: local.path, remote: remote.path }
    }
    return true
  }

  function mirrorToLocal(path) {
    if (mirroring || !anchorMirror()) return
    const target = mirrorPath(mirrorBases.remote, path, mirrorBases.local, { posix: false })
    if (!target || target === local.path) return
    mirroring = true
    loadLocal(target).finally(() => (mirroring = false))
  }

  function mirrorToRemote(path) {
    if (mirroring || !anchorMirror()) return
    const target = mirrorPath(mirrorBases.local, path, mirrorBases.remote, { posix: true })
    if (!target || target === remote.path) return
    mirroring = true
    loadRemote(target).finally(() => (mirroring = false))
  }

  // ----- comparison -----
  async function compare() {
    report_('Comparing both sides…')
    await guard(async () => {
      report = await transferApi.compare(tab.sessionId, local.path, remote.path, true)
      showCompare = true
      report_(`${report.summary} — compared by ${report.compared_by}.`)
    })
  }

  const localMarks = $derived.by(() =>
    marksFor(report, report?.local_dir, local.path, local.entries, false),
  )
  const remoteMarks = $derived.by(() =>
    marksFor(report, report?.remote_dir, remote.path, remote.entries, true),
  )

  const SYMBOLS = {
    same: '=',
    different: '≠',
    local_only: '→',
    remote_only: '←',
    unknown: '?',
  }

  function marksFor(current, baseDir, currentDir, entries, posix) {
    if (!current || !baseDir || !currentDir) return {}
    const prefix = relativePrefix(baseDir, currentDir, posix)
    if (prefix === null) return {}
    const out = {}
    for (const entry of entries) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name
      const status = entry.is_dir
        ? rollUp(current.statuses, rel)
        : current.statuses[rel]
      if (status) out[entry.name] = SYMBOLS[status] ?? ''
    }
    return out
  }

  function relativePrefix(baseDir, currentDir, posix) {
    const normalise = (value) =>
      value.replace(/\\/g, '/').replace(/\/+$/, '') || (posix ? '' : value)
    const base = normalise(baseDir)
    const here = normalise(currentDir)
    if (base === here) return ''
    const compare = posix ? (text) => text : (text) => text.toLowerCase()
    if (!compare(here).startsWith(compare(base) + '/')) return null
    return here.slice(base.length + 1)
  }

  /** A folder's verdict is its contents': identical only if all of them are. */
  function rollUp(statuses, rel) {
    const prefix = rel + '/'
    const seen = new Set()
    for (const [key, value] of Object.entries(statuses)) {
      if (key.startsWith(prefix)) seen.add(value)
    }
    if (!seen.size) return null
    if (seen.size === 1) return [...seen][0]
    return 'different'
  }

  async function uploadRelative(relatives) {
    if (!relatives.length) return
    if (!confirmProduction(`replace ${relatives.length} file(s) on`)) return
    const base = report?.local_dir || local.path
    const remoteBase = report?.remote_dir || remote.path
    const groups = new Map()
    for (const rel of relatives) {
      const parts = rel.split('/')
      parts.pop() // the file name; only its directory decides where it lands
      const target = parts.length ? remotePath.join(remoteBase, parts.join('/')) : remoteBase
      const item = [localPath.join(base, rel.split('/').join('\\')), false]
      groups.set(target, [...(groups.get(target) ?? []), item])
    }
    await guard(async () => {
      for (const [target, items] of groups) {
        await transferApi.upload(tab.sessionId, items, target)
      }
    })
  }

  async function downloadRelative(relatives) {
    if (!relatives.length) return
    const base = report?.remote_dir || remote.path
    const localBase = report?.local_dir || local.path
    const groups = new Map()
    for (const rel of relatives) {
      const parts = rel.split('/')
      parts.pop()
      const target = parts.length ? localPath.join(localBase, parts.join('\\')) : localBase
      const item = [remotePath.join(base, rel), false]
      groups.set(target, [...(groups.get(target) ?? []), item])
    }
    await guard(async () => {
      for (const [target, items] of groups) {
        await transferApi.download(tab.sessionId, items, target)
      }
    })
  }

  // ----- commands -----
  function confirmProduction(action) {
    if (!isProduction) return true
    return window.confirm(
      `You are about to ${action} ${tab.profile.label}, which is marked as production.\n\nGo ahead?`,
    )
  }

  function upload() {
    if (!localSelection.length) return report_('Select something on the left to upload.')
    if (!confirmProduction(`upload ${localSelection.length} item(s) to`)) return
    const items = localSelection.map((item) => [
      localPath.join(local.path, item.name),
      item.is_dir,
    ])
    guard(() => transferApi.upload(tab.sessionId, items, remote.path))
  }

  function download() {
    if (!remoteSelection.length) return report_('Select something on the right to download.')
    const items = remoteSelection.map((item) => [
      remotePath.join(remote.path, item.name),
      item.is_dir,
    ])
    guard(() => transferApi.download(tab.sessionId, items, local.path))
  }

  function newFolder() {
    const name = window.prompt(`Name of the new ${remoteActive ? 'remote' : 'local'} folder:`)
    if (!name?.trim()) return
    guard(async () => {
      if (remoteActive) {
        await transferApi.mkdir(tab.sessionId, remotePath.join(remote.path, name.trim()))
        await loadRemote(remote.path)
      } else {
        await transferApi.localMkdir(localPath.join(local.path, name.trim()))
        await loadLocal(local.path)
      }
    })
  }

  function rename() {
    if (activeSelection.length !== 1) return report_('Select exactly one entry to rename.')
    const current = activeSelection[0].name
    const next = window.prompt('New name:', current)
    if (!next?.trim() || next === current) return
    guard(async () => {
      if (remoteActive) {
        await transferApi.rename(
          tab.sessionId,
          remotePath.join(remote.path, current),
          remotePath.join(remote.path, next.trim()),
        )
        await loadRemote(remote.path)
      } else {
        await transferApi.localRename(
          localPath.join(local.path, current),
          localPath.join(local.path, next.trim()),
        )
        await loadLocal(local.path)
      }
    })
  }

  function remove() {
    if (!activeSelection.length) return report_('Select something to delete.')
    const names = activeSelection.map((item) => item.name).join(', ')
    const folders = activeSelection.some((item) => item.is_dir)
    if (remoteActive && !confirmProduction('delete files on')) return
    if (
      !window.confirm(
        `Delete from the ${remoteActive ? 'remote' : 'local'} side?\n\n${names}` +
          (folders ? '\n\nFolders go with everything inside them.' : ''),
      )
    )
      return
    guard(async () => {
      for (const item of activeSelection) {
        if (remoteActive) {
          await transferApi.remove(
            tab.sessionId,
            remotePath.join(remote.path, item.name),
            item.is_dir,
          )
        } else {
          await transferApi.localRemove(localPath.join(local.path, item.name), item.is_dir)
        }
      }
      await (remoteActive ? loadRemote(remote.path) : loadLocal(local.path))
    })
  }

  function cancel() {
    guard(() => transferApi.cancel(tab.sessionId))
    report_('Cancelling…')
  }

  async function digest() {
    if (remoteSelection.length !== 1 || remoteSelection[0].is_dir) {
      return report_('Select one remote file to hash.')
    }
    await guard(async () => {
      const payload = await transferApi.digest(
        tab.sessionId,
        remotePath.join(remote.path, remoteSelection[0].name),
      )
      report_(payload.digest ? `sha256 ${payload.digest}` : 'Could not hash that file.')
    })
  }

  async function linkTarget() {
    const entry = remote.entries.find((item) => item.name === remoteSelection[0]?.name)
    if (!entry?.is_link) return report_('Select a symbolic link.')
    const next = window.prompt(`${entry.name} points at:`, entry.link_target || '')
    if (!next?.trim() || next === entry.link_target) return
    if (!confirmProduction('change a symlink on')) return
    await guard(async () => {
      await transferApi.symlink(
        tab.sessionId,
        next.trim(),
        remotePath.join(remote.path, entry.name),
      )
      await loadRemote(remote.path)
    })
  }

  async function archive() {
    if (!remoteSelection.length) return report_('Select what to put in the archive.')
    const suggestion =
      (remoteSelection.length === 1 ? remoteSelection[0].name : 'archive') + '.tar.gz'
    const name = window.prompt('Name of the archive to build on the server:', suggestion)
    if (!name?.trim()) return
    await guard(async () => {
      await transferApi.archive(tab.sessionId, {
        directory: remote.path,
        names: remoteSelection.map((item) => item.name),
        archive: remotePath.join(remote.path, name.trim()),
        kind: name.trim().endsWith('.zip') ? 'zip' : 'tar.gz',
      })
      await loadRemote(remote.path)
      report_(`Built ${name.trim()} on the server.`)
    })
  }

  async function extract() {
    if (remoteSelection.length !== 1) return report_('Select one archive to unpack.')
    const destination = window.prompt('Unpack into which directory?', remote.path)
    if (!destination?.trim()) return
    await guard(async () => {
      await transferApi.extract(
        tab.sessionId,
        remotePath.join(remote.path, remoteSelection[0].name),
        destination.trim(),
      )
      await loadRemote(remote.path)
    })
  }

  // ----- replace history -----
  async function loadHistory() {
    await guard(async () => {
      const payload = await transferApi.history(tab.sessionId)
      historyEntries = payload.entries
      showHistory = true
    })
  }

  async function undo(entryId) {
    await guard(async () => {
      const payload = await transferApi.undo(tab.sessionId, entryId)
      report_(payload.message)
      await loadRemote(remote.path)
      await loadHistory()
    })
  }

  async function undoLast() {
    const payload = await transferApi.history(tab.sessionId)
    historyEntries = payload.entries
    const entry = payload.entries.find((item) => item.can_undo)
    if (!entry) return report_('Nothing this app overwrote is still recoverable.')
    if (!window.confirm(`Put the previous version of ${entry.name} back?\n\n${entry.target}`)) return
    await undo(entry.id)
  }

  async function openTerminal() {
    await guard(async () => {
      const payload = await toolsApi.launchTerminal({
        profile_id: tab.profile.id,
        remote_dir: remote.path,
      })
      report_(`Opened ${payload.started}.`)
    })
  }
</script>

<div class="manager">
  {#if isProduction}
    <div class="warn">PRODUCTION — files here are live.</div>
  {/if}

  <div class="tools-row">
    <label class="faint">
      <input
        type="checkbox"
        bind:checked={mirror}
        onchange={() => {
          if (mirror) {
            mirrorBases = { local: local.path, remote: remote.path }
          }
        }}
      /> mirror
    </label>
    <span class="divider" aria-hidden="true"></span>
    <button class="btn" onclick={compare}>Compare</button>
    <button class="btn" class:primary={showQueue} onclick={() => (showQueue = !showQueue)}>
      Queue{queueItems.length ? ` (${queueItems.length})` : ''}
    </button>
    <span class="divider" aria-hidden="true"></span>
    <button class="btn" onclick={loadHistory}>History</button>
    <button class="btn" onclick={undoLast}>Undo replace</button>
    <div class="grow"></div>
    <button
      class="btn"
      class:primary={showTools}
      title={canExec ? '' : 'This connection has no shell'}
      onclick={() => (showTools = !showTools)}>Server tools</button
    >
    <button class="btn" onclick={openTerminal}>Terminal</button>
  </div>

  <div class="panes">
    <FilePane
      title="Local"
      path={local.path}
      entries={local.entries}
      atRoot={local.at_root}
      active={!remoteActive}
      {busy}
      marks={localMarks}
      canGoBack={localNav.back}
      canGoForward={localNav.forward}
      recent={localNav.recent}
      bind:selection={localSelection}
      onNavigate={navigateLocal}
      onEnter={(name) => loadLocal(localPath.join(local.path, name))}
      onFocus={() => (remoteActive = false)}
      onBack={() => replay(loadLocal, localHistory.back())}
      onForward={() => replay(loadLocal, localHistory.forward())}
    />

    <div class="middle">
      <button class="btn" title="Upload to the remote side" onclick={upload}>▲</button>
      <button class="btn" title="Download to the local side" onclick={download}>▼</button>
    </div>

    <FilePane
      title={`Remote — ${tab.profile.label}`}
      path={remote.path}
      entries={remote.entries}
      atRoot={remote.at_root}
      active={remoteActive}
      {busy}
      marks={remoteMarks}
      canGoBack={remoteNav.back}
      canGoForward={remoteNav.forward}
      recent={remoteNav.recent}
      bind:selection={remoteSelection}
      onNavigate={navigateRemote}
      onEnter={(name) => loadRemote(remotePath.join(remote.path, name))}
      onFocus={() => (remoteActive = true)}
      onBack={() => replay(loadRemote, remoteHistory.back())}
      onForward={() => replay(loadRemote, remoteHistory.forward())}
    />
  </div>

  {#if showCompare && report}
    <CompareView
      {report}
      onUpload={uploadRelative}
      onDownload={downloadRelative}
      onRefresh={compare}
      onClose={() => (showCompare = false)}
    />
  {/if}

  {#if showQueue}
    <TransferQueue
      items={queueItems}
      stats={queueStats}
      onPause={() => transferApi.pause(tab.sessionId).then(refreshQueue)}
      onResume={() => transferApi.resume(tab.sessionId).then(refreshQueue)}
      onCancelAll={() => transferApi.cancel(tab.sessionId).then(refreshQueue)}
      onClearFinished={() =>
        transferApi.clearFinished(tab.sessionId).then(() => {
          queueItems = queueItems.filter(
            (item) => !['done', 'cancelled', 'skipped'].includes(item.state),
          )
          refreshQueue()
        })}
      onCancelItem={(id) => transferApi.cancelItem(tab.sessionId, id).then(refreshQueue)}
      onPrioritize={(id) => transferApi.prioritize(tab.sessionId, id).then(refreshQueue)}
      onReorder={(ids) => transferApi.reorder(tab.sessionId, ids).then(refreshQueue)}
      onWorkers={(count) =>
        transferApi.setOptions(tab.sessionId, { workers: count }).then(refreshQueue)}
    />
  {/if}

  {#if showTools}
    <RemoteTools
      sessionId={tab.sessionId}
      remoteDir={remote.path}
      localDir={local.path}
      host={tab.profile.host}
      user={tab.profile.username}
      selection={remoteSelection}
      {canExec}
      bind:tool={toolView}
      onNavigate={(path) => loadRemote(path.endsWith('/') ? path : remotePath.parent(path))}
      onStatus={report_}
      onClose={() => (showTools = false)}
    />
  {/if}

  {#if showHistory}
    <section class="history">
      <header>
        <strong>Replace history</strong>
        <div class="grow"></div>
        <button class="btn" onclick={() => (showHistory = false)}>Close</button>
      </header>
      {#each historyEntries as entry (entry.id)}
        <div class="line">
          <span class="truncate">{entry.describe}</span>
          <span class="truncate faint mono">{entry.target}</span>
          <span class="faint">{humanSize(entry.size)}</span>
          <button class="btn" disabled={!entry.can_undo} onclick={() => undo(entry.id)}>
            {entry.undone ? 'restored' : 'Restore'}
          </button>
        </div>
      {/each}
      {#if !historyEntries.length}
        <p class="faint">Nothing has been overwritten yet.</p>
      {/if}
    </section>
  {/if}

  <div class="toolbar">
    <button class="btn" onclick={newFolder}>New folder</button>
    <button class="btn" onclick={rename}>Rename</button>
    <button class="btn danger" onclick={remove}>Delete</button>
    <span class="divider" aria-hidden="true"></span>
    <button class="btn" onclick={digest}>Digest</button>
    <button class="btn" onclick={linkTarget}>Link target</button>
    <button class="btn" disabled={!canExec} onclick={archive}>Archive</button>
    <button class="btn" disabled={!canExec} onclick={extract}>Unpack</button>
    <div class="grow"></div>
    {#if progress}
      <span class="faint mono">{progress.done}/{progress.total} {progress.name}</span>
      <div class="bar"><div class="fill" style:width={`${progress.percent}%`}></div></div>
      <button class="btn" onclick={cancel}>Cancel</button>
    {/if}
  </div>

  <p class="status faint truncate">{status}</p>
</div>

<style>
  .manager {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    gap: 8px;
    padding: 10px;
  }

  .warn {
    background: var(--red);
    color: #fff;
    font-weight: 600;
    padding: 5px 10px;
    border-radius: var(--radius-sm);
  }

  .tools-row,
  .toolbar {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 11.5px;
  }

  /* The header used to run straight into the panes, which read as one
     undifferentiated field. Both bars now have an edge. */
  .tools-row {
    padding: 0 2px 8px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 2px;
  }

  .toolbar {
    padding: 8px 2px 0;
    border-top: 1px solid var(--border);
    margin-top: 2px;
  }

  .divider {
    width: 1px;
    align-self: stretch;
    min-height: 20px;
    background: var(--border);
    margin: 0 2px;
  }

  .panes {
    flex: 1;
    display: flex;
    gap: 8px;
    min-height: 0;
  }

  .middle {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 8px;
    flex: 0 0 auto;
  }

  .history {
    display: flex;
    flex-direction: column;
    gap: 4px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    padding: 8px;
    max-height: 220px;
    overflow: auto;
    font-size: 11.5px;
  }

  .history header {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .history .line {
    display: grid;
    grid-template-columns: minmax(0, 32%) minmax(0, 1fr) 70px auto;
    gap: 8px;
    align-items: center;
  }

  .bar {
    width: 160px;
    height: 6px;
    background: var(--panel-2);
    border-radius: 999px;
    overflow: hidden;
  }

  .fill {
    height: 100%;
    background: var(--accent);
    transition: width 0.15s linear;
  }

  .status {
    margin: 0;
    font-size: 11.5px;
    min-height: 15px;
  }
</style>
