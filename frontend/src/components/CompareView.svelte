<script>
  // What differs between here and the server. The verdict comes from digests,
  // so "identical" means identical - and each row can be sent either way.
  let { report = null, onUpload, onDownload, onRefresh, onClose } = $props()

  const GROUPS = [
    { status: 'different', heading: 'Different', preTicked: true },
    { status: 'local_only', heading: 'Only here', preTicked: true },
    { status: 'remote_only', heading: 'Only on the server', preTicked: false },
    { status: 'unknown', heading: 'Cannot tell', preTicked: false },
    { status: 'same', heading: 'Identical', preTicked: false },
  ]

  let ticked = $state(new Set())
  let showSame = $state(false)
  let seen = $state('')

  const grouped = $derived.by(() => {
    const out = {}
    for (const group of GROUPS) out[group.status] = []
    for (const [rel, status] of Object.entries(report?.statuses ?? {})) {
      if (out[status]) out[status].push(rel)
    }
    for (const list of Object.values(out)) list.sort()
    return out
  })

  // Re-tick the sensible default whenever a fresh comparison arrives.
  $effect(() => {
    const stamp = report?.summary ?? ''
    if (stamp === seen) return
    seen = stamp
    const fresh = new Set()
    for (const group of GROUPS) {
      if (!group.preTicked) continue
      for (const rel of grouped[group.status] ?? []) fresh.add(rel)
    }
    ticked = fresh
  })

  function toggle(rel) {
    const next = new Set(ticked)
    if (next.has(rel)) next.delete(rel)
    else next.add(rel)
    ticked = next
  }

  function setAll(on) {
    if (!on) {
      ticked = new Set()
      return
    }
    const next = new Set()
    for (const group of GROUPS) {
      if (group.status === 'same' && !showSame) continue
      for (const rel of grouped[group.status] ?? []) next.add(rel)
    }
    ticked = next
  }

  function chosen(excludeStatus) {
    return [...ticked].filter((rel) => report?.statuses?.[rel] !== excludeStatus)
  }
</script>

<section class="compare">
  <header>
    <strong>Compare</strong>
    <span class="faint truncate">
      {report?.summary ?? ''} — compared by {report?.compared_by ?? 'hash'}
    </span>
    <div class="grow"></div>
    <button class="btn" onclick={() => onRefresh?.()}>Compare again</button>
    <button class="btn" onclick={() => onClose?.()}>Close</button>
  </header>

  <div class="lists">
    {#each GROUPS as group (group.status)}
      {#if grouped[group.status]?.length && (group.status !== 'same' || showSame)}
        <div class="group">
          <div class="heading {group.status}">
            {group.heading} ({grouped[group.status].length})
          </div>
          {#each grouped[group.status] as rel (rel)}
            <label class="line">
              <input type="checkbox" checked={ticked.has(rel)} onchange={() => toggle(rel)} />
              <span class="truncate mono">{rel}</span>
            </label>
          {/each}
        </div>
      {/if}
    {/each}
    {#if !Object.keys(report?.statuses ?? {}).length}
      <p class="faint">Nothing to compare - both sides are empty.</p>
    {/if}
  </div>

  <footer>
    <label class="faint">
      <input type="checkbox" bind:checked={showSame} /> show identical
    </label>
    <button class="btn" onclick={() => setAll(true)}>Tick all shown</button>
    <button class="btn" onclick={() => setAll(false)}>Untick all</button>
    <div class="grow"></div>
    <button class="btn primary" onclick={() => onUpload?.(chosen('remote_only'))}>
      ▲ Upload ticked
    </button>
    <button class="btn" onclick={() => onDownload?.(chosen('local_only'))}>
      ▼ Download ticked
    </button>
  </footer>
</section>

<style>
  .compare {
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
  footer {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11.5px;
  }

  .lists {
    overflow: auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .heading {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 2px;
  }

  .heading.different {
    color: var(--amber);
  }

  .heading.local_only,
  .heading.remote_only {
    color: var(--accent);
  }

  .heading.same {
    color: var(--green);
  }

  .line {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11.5px;
    padding: 1px 2px;
  }

  .line:hover {
    background: var(--card-hover);
    border-radius: 4px;
  }
</style>
