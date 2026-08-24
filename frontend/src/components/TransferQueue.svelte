<script>
  // The transfer queue: what is running, what is waiting, and control over
  // both. Rows can be cancelled on their own, pushed to the front, or dragged
  // into the order you want.
  import { humanSize } from '../lib/format.js'

  let {
    items = [],
    stats = {},
    onPause,
    onResume,
    onCancelAll,
    onClearFinished,
    onCancelItem,
    onPrioritize,
    onReorder,
    onWorkers,
  } = $props()

  let dragging = $state(null)

  const paused = $derived(Boolean(stats?.paused))
  const counts = $derived(stats?.counts ?? {})
  const workers = $derived(stats?.workers ?? 3)

  const summary = $derived.by(() => {
    const parts = [
      `${counts.running ?? 0} running`,
      `${counts.queued ?? 0} waiting`,
      `${counts.done ?? 0} done`,
    ]
    if (counts.failed) parts.push(`${counts.failed} failed`)
    if (stats?.bytes_total) {
      parts.push(`${humanSize(stats.bytes_done ?? 0)} of ${humanSize(stats.bytes_total)}`)
    }
    return parts.join('  ·  ')
  })

  function percent(item) {
    if (item.state === 'done') return 100
    return Math.round((item.fraction ?? 0) * 100)
  }

  function drop(target) {
    if (!dragging || dragging === target.id) return
    const order = items.map((item) => item.id)
    const from = order.indexOf(dragging)
    const to = order.indexOf(target.id)
    if (from < 0 || to < 0) return
    order.splice(to, 0, ...order.splice(from, 1))
    dragging = null
    onReorder?.(order)
  }
</script>

<section class="queue">
  <header>
    <span class="faint truncate">{summary}</span>
    <div class="grow"></div>
    <label class="workers">
      At once
      <input
        type="number"
        min="1"
        max="16"
        value={workers}
        onchange={(event) => onWorkers?.(Number(event.currentTarget.value))}
      />
    </label>
    <button class="btn" onclick={() => (paused ? onResume?.() : onPause?.())}>
      {paused ? 'Resume' : 'Pause'}
    </button>
    <button class="btn" onclick={() => onCancelAll?.()}>Cancel all</button>
    <button class="btn" onclick={() => onClearFinished?.()}>Clear finished</button>
  </header>

  <div class="rows">
    {#each items as item (item.id)}
      <div
        class="row"
        class:done={item.state === 'done'}
        class:failed={item.state === 'failed'}
        draggable={item.state === 'queued'}
        role="listitem"
        ondragstart={() => (dragging = item.id)}
        ondragover={(event) => event.preventDefault()}
        ondrop={() => drop(item)}
      >
        <span class="truncate" title={`${item.local}  →  ${item.remote}`}>
          {item.upload ? '▲' : '▼'} {item.name}
        </span>
        <span class="faint right">{humanSize(item.size)}</span>
        <div class="bar" aria-hidden="true">
          <div class="fill" style:width={`${percent(item)}%`}></div>
        </div>
        <span class="faint state truncate">{item.error || item.note || item.state}</span>
        <span class="actions">
          {#if item.state === 'queued'}
            <button class="btn icon" title="Transfer next" onclick={() => onPrioritize?.(item.id)}
              >⤒</button
            >
          {/if}
          {#if item.state === 'queued' || item.state === 'running'}
            <button class="btn icon" title="Cancel" onclick={() => onCancelItem?.(item.id)}>✕</button>
          {/if}
        </span>
      </div>
    {/each}
    {#if !items.length}
      <p class="empty faint">The queue is empty.</p>
    {/if}
  </div>
</section>

<style>
  .queue {
    display: flex;
    flex-direction: column;
    gap: 6px;
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    background: var(--panel);
    padding: 6px 8px;
    max-height: 210px;
  }

  header {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11.5px;
  }

  .workers {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--text-dim);
  }

  .workers input {
    width: 48px;
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 2px 4px;
  }

  .rows {
    overflow: auto;
    min-height: 0;
  }

  .row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 70px 90px 120px 54px;
    gap: 8px;
    align-items: center;
    padding: 2px 4px;
    font-size: 11.5px;
    border-radius: 4px;
  }

  .row:hover {
    background: var(--card-hover);
  }

  .row.done .state {
    color: var(--green);
  }

  .row.failed .state {
    color: var(--red);
  }

  .bar {
    height: 5px;
    background: var(--panel-2);
    border-radius: 999px;
    overflow: hidden;
  }

  .fill {
    height: 100%;
    background: var(--accent);
    transition: width 0.15s linear;
  }

  .actions {
    display: flex;
    gap: 2px;
    justify-content: flex-end;
  }

  .right {
    text-align: right;
  }

  .empty {
    margin: 0;
    padding: 6px 2px;
    font-size: 11.5px;
  }
</style>
