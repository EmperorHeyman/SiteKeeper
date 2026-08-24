<script>
  // Grouped, searchable connection list - the left column of the window.
  import { ENV_COLOR, KIND_BADGE } from '../lib/format.js'

  let {
    groups = [],
    selectedId = null,
    onSelect,
    onOpen,
    onEdit,
    onDelete,
    onAdd,
    onCollapse,
  } = $props()

  let search = $state('')
  let collapsedGroups = $state({})

  const filtered = $derived.by(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return groups
    return groups
      .map((group) => ({
        ...group,
        servers: group.servers.filter(
          (server) =>
            server.label.toLowerCase().includes(needle) ||
            (server.target ?? '').toLowerCase().includes(needle) ||
            (server.group ?? '').toLowerCase().includes(needle),
        ),
      }))
      .filter((group) => group.servers.length)
  })

  const total = $derived(groups.reduce((sum, group) => sum + group.servers.length, 0))

  function toggleGroup(name) {
    collapsedGroups[name] = !collapsedGroups[name]
  }
</script>

<aside class="sidebar surface">
  <header>
    <span class="title">Connections</span>
    <span class="badge">{total}</span>
    <div class="grow"></div>
    <button class="btn icon" title="Collapse (Ctrl+B)" onclick={onCollapse}>«</button>
  </header>

  <div class="search">
    <input
      placeholder="Search connections…"
      bind:value={search}
      spellcheck="false"
    />
  </div>

  <div class="list">
    {#each filtered as group (group.name)}
      <button class="group" onclick={() => toggleGroup(group.name)}>
        <span class="chev">{collapsedGroups[group.name] ? '›' : '⌄'}</span>
        <span class="truncate">{group.name}</span>
        <span class="faint">{group.servers.length}</span>
      </button>

      {#if !collapsedGroups[group.name]}
        {#each group.servers as server (server.id)}
          <div
            class="item"
            class:selected={server.id === selectedId}
            role="button"
            tabindex="0"
            onclick={() => onSelect?.(server)}
            ondblclick={() => onOpen?.(server)}
            onkeydown={(event) => {
              if (event.key === 'Enter') onOpen?.(server)
            }}
          >
            <span
              class="dot"
              style:background={ENV_COLOR[server.environment] || 'transparent'}
              style:border-color={ENV_COLOR[server.environment] || 'var(--border)'}
            ></span>
            <span class="grow truncate">
              <span class="label">{server.label}</span>
              <span class="target truncate">{server.target}</span>
            </span>
            <span class="badge">{KIND_BADGE[server.kind] ?? server.kind}</span>
          </div>
        {/each}
      {/if}
    {/each}

    {#if !filtered.length}
      <p class="empty faint">
        {total ? 'Nothing matches that search.' : 'No connections saved yet.'}
      </p>
    {/if}
  </div>

  <footer>
    <button class="btn primary grow" onclick={onAdd}>Add</button>
    <button class="btn" disabled={!selectedId} onclick={onEdit}>Edit</button>
    <button class="btn danger" disabled={!selectedId} onclick={onDelete}>Delete</button>
  </footer>
</aside>

<style>
  .sidebar {
    width: 300px;
    flex: 0 0 300px;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 12px 8px;
  }

  .title {
    font-weight: 600;
  }

  .search {
    padding: 0 12px 10px;
  }

  .list {
    flex: 1;
    overflow-y: auto;
    padding: 0 8px 8px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-height: 0;
  }

  .group {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    padding: 7px 8px 5px;
    color: var(--text-faint);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    text-align: left;
  }

  .group:hover {
    color: var(--text-dim);
  }

  .chev {
    width: 10px;
    font-size: 10px;
  }

  .item {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 9px 10px;
    border-radius: 10px;
    background: var(--card);
    border: 1px solid transparent;
    cursor: pointer;
    transition: background 0.1s, border-color 0.1s;
  }

  .item:hover {
    background: var(--card-hover);
  }

  .item.selected {
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  .dot {
    width: 8px;
    height: 8px;
    flex: 0 0 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
  }

  .label {
    display: block;
    font-weight: 500;
  }

  .target {
    display: block;
    color: var(--text-faint);
    font-size: 11px;
  }

  .empty {
    padding: 18px 10px;
    text-align: center;
  }

  footer {
    display: flex;
    gap: 6px;
    padding: 10px 12px 12px;
    border-top: 1px solid var(--border-soft);
  }
</style>
