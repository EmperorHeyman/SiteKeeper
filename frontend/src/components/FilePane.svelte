<script>
  // One side of the dual pane. Identical markup for local and remote; only the
  // data source and the path arithmetic differ, both injected by the parent.
  import { humanSize, humanTime } from '../lib/format.js'

  let {
    title = '',
    path = '',
    entries = [],
    atRoot = false,
    active = false,
    busy = false,
    selection = $bindable([]),
    // Comparison verdict per entry name: '=' , '≠', '→', '←' or ''.
    marks = {},
    // Back / forward availability and the recent list come from the parent's
    // NavHistory, so the two panes keep separate memories.
    canGoBack = false,
    canGoForward = false,
    recent = [],
    onNavigate,
    onEnter,
    onFocus,
    onBack,
    onForward,
    onContext,
  } = $props()

  // Starts empty and is filled by the effect below, so it tracks every later
  // navigation instead of freezing on the first path.
  let draft = $state('')
  let showRecent = $state(false)

  $effect(() => {
    draft = path
  })

  function toggle(name, isDir, event) {
    if (event.ctrlKey || event.metaKey) {
      selection = selection.some((item) => item.name === name)
        ? selection.filter((item) => item.name !== name)
        : [...selection, { name, is_dir: isDir }]
    } else {
      selection = [{ name, is_dir: isDir }]
    }
    onFocus?.()
  }

  function isSelected(name) {
    return selection.some((item) => item.name === name)
  }

  function markClass(name) {
    const mark = marks[name]
    if (mark === '=') return 'same'
    if (mark === '≠') return 'differs'
    if (mark === '→' || mark === '←') return 'only'
    return ''
  }

  function label(entry) {
    const icon = entry.is_dir ? '📁' : '📄'
    if (entry.is_link) {
      return `${icon} ${entry.name} →${entry.link_target ? ' ' + entry.link_target : ''}`
    }
    return `${icon} ${entry.name}`
  }

  function octal(mode) {
    if (mode === null || mode === undefined) return ''
    return (mode & 0o7777).toString(8).padStart(3, '0')
  }
</script>

<section class="pane" class:active role="group" onfocusin={onFocus}>
  <header>
    <span class="name truncate">{title}</span>
    <div class="grow"></div>
    <button
      class="btn icon"
      title="Back (Alt+Left)"
      disabled={!canGoBack || busy}
      onclick={() => onBack?.()}>◀</button
    >
    <button
      class="btn icon"
      title="Forward (Alt+Right)"
      disabled={!canGoForward || busy}
      onclick={() => onForward?.()}>▶</button
    >
    <div class="recent-host">
      <button
        class="btn icon"
        title="Recently visited"
        disabled={!recent.length || busy}
        onclick={() => (showRecent = !showRecent)}>▾</button
      >
      {#if showRecent}
        <ul class="recent">
          {#each recent as entry (entry)}
            <li>
              <button
                class="recent-item truncate"
                onclick={() => {
                  showRecent = false
                  onNavigate?.(entry)
                }}>{entry}</button
              >
            </li>
          {/each}
        </ul>
      {/if}
    </div>
    <button
      class="btn icon"
      title="Parent directory (Alt+Up)"
      disabled={atRoot || busy}
      onclick={() => onNavigate?.('..')}>↑</button
    >
    <button class="btn icon" title="Refresh (F5)" disabled={busy} onclick={() => onNavigate?.(path)}>
      ⟳
    </button>
  </header>

  <input
    class="mono path"
    bind:value={draft}
    spellcheck="false"
    onkeydown={(event) => {
      if (event.key === 'Enter') onNavigate?.(draft.trim())
    }}
  />

  <div class="table" role="listbox" tabindex="-1">
    <div class="head row-grid faint">
      <span>Name</span><span class="right">Size</span><span>Modified</span>
      <span class="right">Mode</span><span class="center">Sync</span>
    </div>

    {#if !atRoot}
      <div
        class="row-grid entry"
        role="option"
        aria-selected="false"
        tabindex="-1"
        ondblclick={() => onNavigate?.('..')}
        onkeydown={(event) => {
          if (event.key === 'Enter') onNavigate?.('..')
        }}
      >
        <span class="truncate">..</span><span></span><span></span><span></span><span></span>
      </div>
    {/if}

    {#each entries as entry (entry.name)}
      <div
        class="row-grid entry"
        class:selected={isSelected(entry.name)}
        class:dir={entry.is_dir}
        class:link={entry.is_link}
        role="option"
        aria-selected={isSelected(entry.name)}
        tabindex="-1"
        onclick={(event) => toggle(entry.name, entry.is_dir, event)}
        oncontextmenu={(event) => {
          event.preventDefault()
          if (!isSelected(entry.name)) toggle(entry.name, entry.is_dir, event)
          onContext?.(entry)
        }}
        ondblclick={() => (entry.is_dir ? onEnter?.(entry.name) : null)}
        onkeydown={(event) => {
          if (event.key === 'Enter') {
            if (entry.is_dir) onEnter?.(entry.name)
          } else if (event.key === ' ') {
            event.preventDefault()
            toggle(entry.name, entry.is_dir, event)
          }
        }}
      >
        <span class="truncate">{label(entry)}</span>
        <span class="right faint">{entry.is_dir && !entry.size ? '' : humanSize(entry.size)}</span>
        <span class="faint">{humanTime(entry.modified)}</span>
        <span class="right faint mono">{octal(entry.mode)}</span>
        <span class="center {markClass(entry.name)}">{marks[entry.name] ?? ''}</span>
      </div>
    {/each}

    {#if !entries.length}
      <p class="empty faint">{busy ? 'Loading…' : 'Empty directory'}</p>
    {/if}
  </div>
</section>

<style>
  .pane {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    background: var(--panel-2);
    border: 1px solid var(--border-soft);
    border-radius: var(--radius);
    padding: 8px;
  }

  .pane.active {
    border-color: var(--accent);
  }

  header {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .recent-host {
    position: relative;
  }

  .recent {
    position: absolute;
    right: 0;
    top: 100%;
    z-index: 20;
    margin: 4px 0 0;
    padding: 4px;
    list-style: none;
    min-width: 260px;
    max-width: 420px;
    max-height: 280px;
    overflow: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    box-shadow: 0 8px 24px rgb(0 0 0 / 35%);
  }

  .recent-item {
    display: block;
    width: 100%;
    text-align: left;
    background: none;
    border: 0;
    color: inherit;
    font: inherit;
    padding: 4px 6px;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .recent-item:hover {
    background: var(--panel-2);
  }

  .name {
    font-weight: 600;
    font-size: 12px;
  }

  .path {
    width: 100%;
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 4px 7px;
    font-size: 11.5px;
  }

  .table {
    flex: 1;
    min-height: 0;
    overflow: auto;
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .row-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 78px 118px 48px 40px;
    gap: 8px;
    padding: 3px 8px;
    font-size: 11.5px;
    align-items: center;
  }

  .head {
    position: sticky;
    top: 0;
    background: var(--panel-2);
    border-bottom: 1px solid var(--border-soft);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    z-index: 1;
  }

  .entry {
    cursor: default;
    border-radius: 4px;
  }

  .entry:hover {
    background: var(--card-hover);
  }

  .entry.selected {
    background: var(--accent-soft);
  }

  .entry.dir .truncate {
    color: var(--text);
    font-weight: 500;
  }

  .empty {
    margin: 0;
    padding: 10px;
    font-size: 11.5px;
  }

  .right {
    text-align: right;
  }

  .center {
    text-align: center;
  }

  .same {
    color: var(--green, #4e9a06);
  }

  .differs {
    color: var(--amber, #c17d11);
    font-weight: 600;
  }

  .only {
    color: var(--accent);
  }

  .entry.link .truncate {
    font-style: italic;
  }
</style>
