// Small display helpers shared by the panes.

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

export function humanSize(bytes) {
  if (!bytes || bytes <= 0) return '0 B'
  let value = bytes
  for (const unit of UNITS) {
    if (value < 1024 || unit === 'TB') {
      return unit === 'B' ? `${Math.round(value)} B` : `${value.toFixed(1)} ${unit}`
    }
    value /= 1024
  }
  return `${value.toFixed(1)} TB`
}

export function humanTime(epochSeconds) {
  if (!epochSeconds) return ''
  const date = new Date(epochSeconds * 1000)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** Environment badge colours, matching the Qt build's tab tinting. */
export const ENV_COLOR = {
  prod: '#e53935',
  staging: '#fb8c00',
  dev: '#4a9eff',
  none: '',
}

export const KIND_LABEL = {
  phpmyadmin: 'phpMyAdmin',
  mysql: 'MySQL',
  sftp: 'SFTP',
  ftp: 'FTP',
  ftps: 'FTPS',
}

export const KIND_BADGE = {
  phpmyadmin: 'pma',
  mysql: 'sql',
  sftp: 'sftp',
  ftp: 'ftp',
  ftps: 'ftps',
}

/** POSIX path helpers for the remote pane. */
export const remotePath = {
  join(base, name) {
    const prefix = (base || '/').replace(/\/+$/, '')
    return `${prefix}/${name}` || '/'
  },
  parent(path) {
    const trimmed = (path || '/').replace(/\/+$/, '')
    const cut = trimmed.lastIndexOf('/')
    if (cut <= 0) return '/'
    return trimmed.slice(0, cut)
  },
}

/** Windows-aware local path helpers. */
export const localPath = {
  join(base, name) {
    const prefix = (base || '').replace(/[\\/]+$/, '')
    return `${prefix}\\${name}`
  },
  parent(path) {
    const trimmed = (path || '').replace(/[\\/]+$/, '')
    const cut = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'))
    if (cut < 0) return trimmed
    const parent = trimmed.slice(0, cut)
    // "C:" alone is not a directory; keep the trailing separator.
    return /^[A-Za-z]:$/.test(parent) ? parent + '\\' : parent
  },
  basename(path) {
    const trimmed = (path || '').replace(/[\\/]+$/, '')
    const cut = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'))
    return cut < 0 ? trimmed : trimmed.slice(cut + 1)
  },
}
