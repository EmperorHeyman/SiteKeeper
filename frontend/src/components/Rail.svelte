<script>
  // The narrow icon strip on the far left. Purely navigation + global toggles;
  // everything it does is handed back to App via callbacks.
  let {
    sidebarCollapsed = false,
    splitView = false,
    onToggleSidebar,
    onToggleSplit,
    onSettings,
    onLock,
    onAdd,
  } = $props()
</script>

<nav class="rail surface">
  <button class="pad accent" title="Add connection" onclick={onAdd}>+</button>

  <div class="divider"></div>

  <button
    class="pad"
    class:active={!sidebarCollapsed}
    title={sidebarCollapsed ? 'Show connections (Ctrl+B)' : 'Collapse connections (Ctrl+B)'}
    onclick={onToggleSidebar}
  >
    {sidebarCollapsed ? '»' : '«'}
  </button>

  <button
    class="pad"
    class:active={splitView}
    title="Split view (Ctrl+Alt+S)"
    onclick={onToggleSplit}
  >
    ⫿⫿
  </button>

  <div class="spacer"></div>

  <button class="pad" title="Settings (Ctrl+,)" onclick={onSettings}>⚙</button>
  <button class="pad" title="Lock now" onclick={onLock}>⏻</button>
</nav>

<style>
  .rail {
    width: 46px;
    flex: 0 0 46px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    padding: 10px 0;
  }

  .pad {
    width: 32px;
    height: 32px;
    display: grid;
    place-items: center;
    border-radius: 9px;
    color: var(--text-faint);
    font-size: 14px;
    transition: background 0.12s, color 0.12s;
  }

  .pad:hover {
    background: var(--card-hover);
    color: var(--text);
  }

  .pad.active {
    background: var(--accent-soft);
    color: var(--accent);
  }

  .pad.accent {
    background: var(--accent);
    color: #06121f;
    font-size: 18px;
    font-weight: 600;
  }

  .pad.accent:hover {
    background: #63adff;
  }

  .divider {
    width: 20px;
    height: 1px;
    background: var(--border);
    margin: 4px 0;
  }

  .spacer {
    flex: 1;
  }
</style>
