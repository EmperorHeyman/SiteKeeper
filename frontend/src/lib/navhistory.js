// Back / forward memory for one file pane - the browser behaviour: visiting a
// directory pushes it, Back walks towards older entries without forgetting the
// newer ones, and visiting something new while parked in the middle drops the
// forward tail. Mirrors mysql_runner/transfer/navhistory.py.

const LIMIT = 200

export class NavHistory {
  #entries = []
  #index = -1
  #limit

  constructor(limit = LIMIT) {
    this.#limit = Math.max(2, limit)
  }

  get current() {
    return this.#index >= 0 ? this.#entries[this.#index] : ''
  }

  get entries() {
    return [...this.#entries]
  }

  get index() {
    return this.#index
  }

  get canGoBack() {
    return this.#index > 0
  }

  get canGoForward() {
    return this.#index > -1 && this.#index < this.#entries.length - 1
  }

  /** Record a directory the user navigated to. A refresh is not a navigation. */
  visit(path) {
    if (!path || this.current === path) return
    this.#entries.splice(this.#index + 1)
    this.#entries.push(path)
    if (this.#entries.length > this.#limit) {
      this.#entries.splice(0, this.#entries.length - this.#limit)
    }
    this.#index = this.#entries.length - 1
  }

  back() {
    if (!this.canGoBack) return ''
    this.#index -= 1
    return this.#entries[this.#index]
  }

  forward() {
    if (!this.canGoForward) return ''
    this.#index += 1
    return this.#entries[this.#index]
  }

  go(index) {
    if (index < 0 || index >= this.#entries.length) return ''
    this.#index = index
    return this.#entries[this.#index]
  }

  /** Most recently visited directories, newest first, without duplicates. */
  recent(count = 10) {
    const seen = []
    for (let i = this.#index; i >= 0; i -= 1) {
      const path = this.#entries[i]
      if (!seen.includes(path)) seen.push(path)
      if (seen.length >= count) break
    }
    return seen
  }
}

/**
 * Translate a path under one base into the matching path under another - what
 * mirrored navigation needs. Returns '' when the path is not inside the base.
 */
export function mirrorPath(sourceBase, sourcePath, targetBase, { posix }) {
  const rel = relative(sourceBase, sourcePath)
  if (rel === null) return ''
  if (!rel) return targetBase
  if (posix) {
    return (targetBase === '/' ? '' : targetBase.replace(/\/+$/, '')) + '/' + rel
  }
  const separator = targetBase.includes('/') && !targetBase.includes('\\') ? '/' : '\\'
  return targetBase.replace(/[\\/]+$/, '') + separator + rel.split('/').join(separator)
}

function split(path) {
  return path
    .replace(/\\/g, '/')
    .split('/')
    .filter((part) => part !== '' && part !== '.')
}

function relative(base, path) {
  const baseParts = split(base)
  const pathParts = split(path)
  if (pathParts.length < baseParts.length) return null
  for (let i = 0; i < baseParts.length; i += 1) {
    // Windows paths are case-insensitive, and comparing them as given would
    // break mirroring the moment a drive letter's case differed.
    if (baseParts[i].toLowerCase() !== pathParts[i].toLowerCase()) return null
  }
  return pathParts.slice(baseParts.length).join('/')
}
