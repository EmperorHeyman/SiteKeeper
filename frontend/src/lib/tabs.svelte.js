// Open sessions, and which pane each one lives in.
//
// Split view is modelled by giving every tab a pane index (0 or 1) rather than
// keeping two separate lists, so moving a tab across is a single field change
// and nothing can be dropped on the way.

import { sqlApi, transferApi } from './api.js'

let nextLocalId = 1

export const tabs = $state({
  items: [],
  /** Active tab id per pane. */
  active: { 0: null, 1: null },
  splitView: false,
  /** Pane that new tabs open in. */
  focusedPane: 0,
})

export function paneTabs(pane) {
  return tabs.items.filter((tab) => tab.pane === pane)
}

export function activeTab(pane = tabs.focusedPane) {
  const id = tabs.active[pane]
  return tabs.items.find((tab) => tab.id === id) ?? null
}

function adopt(tab) {
  tabs.items.push(tab)
  tabs.active[tab.pane] = tab.id
  return tab
}

/** Open a connection, choosing the tab type from the profile's kind. */
export async function openProfile(profile) {
  const pane = tabs.splitView ? tabs.focusedPane : 0
  const base = {
    id: `t${nextLocalId++}`,
    pane,
    profile,
    title: profile.label,
    status: 'connecting',
    error: '',
    sessionId: null,
  }

  if (profile.kind === 'mysql') {
    const tab = adopt({ ...base, kind: 'sql' })
    try {
      const info = await sqlApi.open(profile.id)
      tab.sessionId = info.session_id
      tab.status = 'open'
      tab.banner =
        `Connected to ${info.target} as ${profile.username}.\n` +
        `Server version: ${info.server_version}   Connection id: ${info.connection_id}   ` +
        `Database: ${info.database || '(none)'}`
      tab.database = info.database
    } catch (error) {
      tab.status = 'failed'
      tab.error = error.detail || error.message
    }
    return tab
  }

  if (profile.is_transfer) {
    const tab = adopt({ ...base, kind: 'transfer' })
    try {
      const info = await transferApi.open(profile.id)
      tab.sessionId = info.session_id
      tab.status = 'open'
      tab.banner = info.banner
    } catch (error) {
      tab.status = 'failed'
      tab.error = error.detail || error.message
    }
    return tab
  }

  // phpMyAdmin: a real browser view is the shell's job, so until the isolated
  // webview lands this hands the URL to the system browser.
  const tab = adopt({ ...base, kind: 'web', status: 'open' })
  return tab
}

export async function closeTab(id) {
  const index = tabs.items.findIndex((tab) => tab.id === id)
  if (index < 0) return
  const [tab] = tabs.items.splice(index, 1)
  if (tabs.active[tab.pane] === id) {
    const remaining = paneTabs(tab.pane)
    tabs.active[tab.pane] = remaining.length ? remaining[remaining.length - 1].id : null
  }
  // Free the backend session; a failure here is not worth blocking the UI.
  try {
    if (tab.kind === 'sql' && tab.sessionId) await sqlApi.close(tab.sessionId)
    if (tab.kind === 'transfer' && tab.sessionId) await transferApi.close(tab.sessionId)
  } catch {
    /* already gone */
  }
}

export function focusTab(pane, id) {
  tabs.active[pane] = id
  tabs.focusedPane = pane
}

export function setSplitView(enabled) {
  tabs.splitView = enabled
  if (!enabled) {
    // Everything comes back to the first pane rather than being closed.
    for (const tab of tabs.items) tab.pane = 0
    const first = paneTabs(0)
    tabs.active[0] = tabs.active[0] ?? (first.length ? first[0].id : null)
    tabs.active[1] = null
    tabs.focusedPane = 0
  }
}

export function moveActiveTabAcross() {
  const tab = activeTab()
  if (!tab) return
  if (!tabs.splitView) setSplitView(true)
  const from = tab.pane
  const to = from === 0 ? 1 : 0
  tab.pane = to
  const left = paneTabs(from)
  tabs.active[from] = left.length ? left[left.length - 1].id : null
  tabs.active[to] = tab.id
  tabs.focusedPane = to
}

export async function closeAll() {
  for (const tab of [...tabs.items]) await closeTab(tab.id)
}
