// Talking to the Python sidecar.
//
// The Tauri shell picks the port and a per-launch token and hands them over
// through the `backend_info` command. Running in a plain browser (vite dev with
// no shell) falls back to the development default so the UI can be worked on
// without building the Rust side.

const DEV_FALLBACK = { base: 'http://127.0.0.1:8766', token: '' }

let backend = null

function inTauri() {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

/** Resolve (once) where the backend is and how to authenticate to it. */
export async function connect() {
  if (backend) return backend
  if (!inTauri()) {
    backend = DEV_FALLBACK
    return backend
  }
  const { invoke } = await import('@tauri-apps/api/core')
  backend = await invoke('backend_info')
  return backend
}

export function backendInfo() {
  return backend ?? DEV_FALLBACK
}

/** Error carrying the HTTP status, so callers can treat 423 (locked) specially. */
export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `request failed (${status})`)
    this.status = status
    this.detail = detail
  }
}

async function request(method, path, body, options = {}) {
  const { base, token } = await connect()
  const headers = {}
  if (token) headers['X-MySQLRunner-Token'] = token
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response
  try {
    response = await fetch(base + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: options.signal,
    })
  } catch (cause) {
    throw new ApiError(0, 'the backend is not responding')
  }

  if (response.status === 204) return null
  const text = await response.text()
  const payload = text ? safeJson(text) : null
  if (!response.ok) {
    throw new ApiError(response.status, payload?.detail ?? text ?? '')
  }
  return payload
}

function safeJson(text) {
  try {
    return JSON.parse(text)
  } catch {
    return { detail: text }
  }
}

export const api = {
  get: (path, options) => request('GET', path, undefined, options),
  post: (path, body, options) => request('POST', path, body ?? {}, options),
  put: (path, body) => request('PUT', path, body ?? {}),
  del: (path) => request('DELETE', path),
}

// ----- typed helpers ------------------------------------------------------
export const vaultApi = {
  status: () => api.get('/vault/status'),
  create: (password) => api.post('/vault/create', { password }),
  unlock: (password) => api.post('/vault/unlock', { password }),
  autoUnlock: () => api.post('/vault/auto-unlock'),
  lock: () => api.post('/vault/lock'),
  changePassword: (current_password, new_password) =>
    api.post('/vault/change-password', { current_password, new_password }),
  setProtection: (payload) => api.post('/vault/protection', payload),
}

export const serversApi = {
  list: () => api.get('/servers'),
  read: (id) => api.get(`/servers/${id}`),
  create: (profile) => api.post('/servers', profile),
  update: (id, profile) => api.put(`/servers/${id}`, profile),
  remove: (id) => api.del(`/servers/${id}`),
  exportBundle: (passphrase, path) => api.post('/servers/export', { passphrase, path }),
  importBundle: (passphrase, path) => api.post('/servers/import', { passphrase, path }),
}

export const sqlApi = {
  capabilities: () => api.get('/sql/capabilities'),
  open: (profile_id) => api.post('/sql/open', { profile_id }),
  run: (session_id, sql) => api.post('/sql/run', { session_id, sql }),
  isComplete: (text) => api.post('/sql/complete', { text }),
  close: (session_id) => api.del(`/sql/sessions/${session_id}`),
}

export const transferApi = {
  capabilities: () => api.get('/transfer/capabilities'),
  open: (profile_id) => api.post('/transfer/open', { profile_id }),
  home: (session_id) => api.post('/transfer/home', { session_id, path: '' }),
  list: (session_id, path) => api.post('/transfer/list', { session_id, path }),
  mkdir: (session_id, path) => api.post('/transfer/mkdir', { session_id, path }),
  remove: (session_id, path, is_dir) =>
    api.post('/transfer/delete', { session_id, path, is_dir }),
  rename: (session_id, source, target) =>
    api.post('/transfer/rename', { session_id, source, target }),
  download: (session_id, items, target) =>
    api.post('/transfer/download', { session_id, items, target }),
  upload: (session_id, items, target) =>
    api.post('/transfer/upload', { session_id, items, target }),
  cancel: (session_id) => api.post('/transfer/cancel', { session_id, path: '' }),
  close: (session_id) => api.del(`/transfer/sessions/${session_id}`),
  localList: (path) => api.get(`/transfer/local?path=${encodeURIComponent(path ?? '')}`),
  localHome: () => api.get('/transfer/local/home'),
  localMkdir: (path) => api.post('/transfer/local/mkdir', { path }),
  localRemove: (path, is_dir) => api.post('/transfer/local/delete', { path, is_dir }),
  localRename: (source, target) => api.post('/transfer/local/rename', { source, target }),

  // ----- the queue -----
  queue: (session_id) => api.post('/transfer/queue', { session_id, path: '' }),
  pause: (session_id) => api.post('/transfer/queue/pause', { session_id, path: '' }),
  resume: (session_id) => api.post('/transfer/queue/resume', { session_id, path: '' }),
  cancelItem: (session_id, item_id) =>
    api.post('/transfer/queue/cancel-item', { session_id, item_id }),
  prioritize: (session_id, item_id) =>
    api.post('/transfer/queue/prioritize', { session_id, item_id }),
  reorder: (session_id, item_ids) =>
    api.post('/transfer/queue/reorder', { session_id, item_ids }),
  clearFinished: (session_id) =>
    api.post('/transfer/queue/clear-finished', { session_id, path: '' }),
  setOptions: (session_id, options) =>
    api.post('/transfer/options', { session_id, ...options }),

  // ----- permissions and links -----
  chmod: (session_id, path, mode, recursive = false, scope = 'all') =>
    api.post('/transfer/chmod', { session_id, path, mode, recursive, scope }),
  symlink: (session_id, target, link_path) =>
    api.post('/transfer/symlink', { session_id, target, link_path }),
  linkTarget: (session_id, path) => api.post('/transfer/link-target', { session_id, path }),
  permissionPresets: () => api.get('/transfer/permission-presets'),

  // ----- comparison, statistics -----
  compare: (session_id, local_dir, remote_dir, with_hashes = true) =>
    api.post('/transfer/compare', { session_id, local_dir, remote_dir, with_hashes }),
  folderStats: (session_id, parent, names) =>
    api.post('/transfer/folder-stats', { session_id, parent, names }),
  digest: (session_id, path) => api.post('/transfer/digest', { session_id, path }),

  // ----- server-side tools -----
  grep: (session_id, payload) => api.post('/transfer/grep', { session_id, ...payload }),
  diskUsage: (session_id, path) => api.post('/transfer/disk-usage', { session_id, path }),
  exec: (session_id, command, cwd = '') =>
    api.post('/transfer/exec', { session_id, command, cwd }),
  logs: (session_id, path) => api.post('/transfer/logs', { session_id, path }),
  tail: (session_id, path, lines = 200) =>
    api.post('/transfer/tail', { session_id, path, lines }),
  archive: (session_id, payload) => api.post('/transfer/archive', { session_id, ...payload }),
  extract: (session_id, archive, destination) =>
    api.post('/transfer/extract', { session_id, archive, destination }),

  // ----- replace history -----
  history: (session_id) => api.post('/transfer/history', { session_id, path: '' }),
  undo: (session_id, entry_id) => api.post('/transfer/undo', { session_id, entry_id }),
}

export const toolsApi = {
  snippets: () => api.get('/tools/snippets'),
  saveSnippet: (snippet) => api.post('/tools/snippets', snippet),
  deleteSnippet: (id) => api.del(`/tools/snippets/${id}`),
  restoreSnippets: () => api.post('/tools/snippets/restore-defaults'),
  renderSnippet: (command, context) =>
    api.post('/tools/snippets/render', { command, context }),
  parseConnection: (url, label = '', save = false) =>
    api.post('/tools/connections/parse', { url, label, save }),
  importConnections: (payload) => api.post('/tools/connections/import', payload),
  exportConnections: (payload) => api.post('/tools/connections/export', payload),
  terminals: () => api.get('/tools/terminals'),
  launchTerminal: (payload) => api.post('/tools/terminals/launch', payload),
}

// ----- live events --------------------------------------------------------
const listeners = new Set()
let socket = null

/** Subscribe to backend events. Returns an unsubscribe function. */
export function onBackendEvent(handler) {
  listeners.add(handler)
  openSocket()
  return () => listeners.delete(handler)
}

async function openSocket() {
  if (socket && socket.readyState <= WebSocket.OPEN) return
  const { base, token } = await connect()
  const url = base.replace(/^http/, 'ws') + '/events' + (token ? `?token=${token}` : '')
  socket = new WebSocket(url)
  socket.onmessage = (message) => {
    let parsed
    try {
      parsed = JSON.parse(message.data)
    } catch {
      return
    }
    for (const handler of listeners) handler(parsed.event, parsed.payload ?? {})
  }
  socket.onclose = () => {
    socket = null
    // The sidecar restarts on unlock in some flows; reconnect quietly.
    if (listeners.size) setTimeout(openSocket, 1500)
  }
  socket.onerror = () => socket?.close()
}

/** Poll /health until the sidecar answers, so the UI can wait on startup. */
export async function waitForBackend(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      return await api.get('/health')
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 200))
    }
  }
  throw new ApiError(0, 'the backend did not start in time')
}
