<script>
  // The mysql> console. Statement splitting, result rendering and the
  // "is this statement finished?" test all come from the backend, so the
  // console behaves exactly like the Qt build it replaces.
  import { sqlApi } from '../lib/api.js'

  let { tab } = $props()

  const PROMPT = 'mysql>'
  const CONTINUATION = '->'
  const HELP = `Commands (a statement can also span several lines and end with ;)

  \\?  \\h  help    show this help
  \\c              clear the statement being typed
  \\s              connection status
  \\q              disconnect this console
  clear  cls      clear the screen
  <statement>\\G   run and print each row vertically

Up / Down walks the history. Ctrl+L clears the screen.`

  let lines = $state([])
  let pending = $state([])
  let history = $state([])
  let historyIndex = $state(0)
  let input = $state('')
  let busy = $state(false)
  let transcript

  const prompt = $derived(pending.length ? CONTINUATION : PROMPT)

  function write(text, tone = 'out') {
    lines.push({ text, tone, id: lines.length })
    queueMicrotask(() => {
      if (transcript) transcript.scrollTop = transcript.scrollHeight
    })
  }

  // Greet once the session is up, or explain why it is not.
  $effect(() => {
    if (tab.status === 'open' && !lines.length) {
      write(tab.banner ?? '', 'sys')
      write('Type \\? for help.\n', 'sys')
      if (tab.profile.startup_script?.trim()) {
        submit(tab.profile.startup_script.trim(), { echo: true })
      }
    } else if (tab.status === 'failed' && !lines.length) {
      write(tab.error || 'Could not connect.', 'err')
    }
  })

  function handleMeta(text) {
    const lowered = text.toLowerCase()
    if (lowered === '\\?' || lowered === '\\h' || lowered === 'help') {
      write(HELP + '\n', 'sys')
      return true
    }
    if (lowered === '\\c') {
      pending = []
      return true
    }
    if (lowered === '\\s') {
      write(
        `Connection: ${tab.profile.target}\nProfile:    ${tab.profile.label}\n` +
          `Database:   ${tab.database || '(none)'}\nEnvironment: ${tab.profile.environment}\n`,
        'sys',
      )
      return true
    }
    if (lowered === 'clear' || lowered === 'cls') {
      lines = []
      return true
    }
    if (lowered === '\\q') {
      write('Bye', 'sys')
      return true
    }
    return false
  }

  async function onSubmit() {
    if (busy) return
    const raw = input
    input = ''
    const trimmed = raw.trim()
    if (trimmed) {
      history.push(raw)
      historyIndex = history.length
    }
    write(`${prompt} ${raw}`, 'in')

    // Backslash commands work mid-statement; word aliases only at the start.
    const atStart = pending.length === 0
    if ((atStart || trimmed.startsWith('\\')) && handleMeta(trimmed)) return

    pending.push(raw)
    const buffered = pending.join('\n')

    let complete = false
    try {
      complete = (await sqlApi.isComplete(buffered)).complete
    } catch (error) {
      write(error.detail || error.message, 'err')
      pending = []
      return
    }
    if (!complete) return

    pending = []
    await submit(buffered)
  }

  async function submit(sql, { echo = false } = {}) {
    if (tab.status !== 'open' || !tab.sessionId) {
      write('Not connected.\n', 'err')
      return
    }
    if (echo) write(`${PROMPT} ${sql.replace(/\n/g, ' ')}`, 'in')
    busy = true
    try {
      const response = await sqlApi.run(tab.sessionId, sql)
      tab.database = response.database
      for (const result of response.results) {
        write(result.text, result.error ? 'err' : 'out')
        if (result.truncated) {
          write(
            `(output limited to the first ${result.rowcount} rows — add a LIMIT clause to see a specific slice)`,
            'sys',
          )
        }
        write('')
      }
    } catch (error) {
      write(error.detail || error.message, 'err')
    } finally {
      busy = false
    }
  }

  function onKeydown(event) {
    if (event.key === 'Enter') {
      event.preventDefault()
      onSubmit()
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      if (!history.length) return
      historyIndex = Math.max(0, historyIndex - 1)
      input = history[historyIndex] ?? ''
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (!history.length) return
      historyIndex = Math.min(history.length, historyIndex + 1)
      input = historyIndex === history.length ? '' : history[historyIndex]
    } else if (event.key === 'l' && event.ctrlKey) {
      event.preventDefault()
      lines = []
    }
  }
</script>

<div class="console">
  {#if tab.profile.environment === 'prod'}
    <div class="warn">PRODUCTION — statements run against the live database.</div>
  {/if}

  <div class="transcript mono" bind:this={transcript}>
    {#each lines as line (line.id)}<pre class={line.tone}>{line.text}</pre>{/each}
    {#if busy}<pre class="sys">running…</pre>{/if}
  </div>

  <div class="prompt-row">
    <span class="prompt mono">{prompt}</span>
    <input
      class="mono"
      bind:value={input}
      onkeydown={onKeydown}
      disabled={tab.status !== 'open' || busy}
      placeholder={tab.status === 'open' ? '' : 'not connected'}
      spellcheck="false"
      autocomplete="off"
    />
    <button class="btn" disabled={tab.status !== 'open' || busy} onclick={onSubmit}>
      Run
    </button>
  </div>
</div>

<style>
  .console {
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

  .transcript {
    flex: 1;
    overflow: auto;
    background: #0e1116;
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    min-height: 0;
  }

  pre {
    margin: 0;
    white-space: pre;
    font-size: 12.5px;
    line-height: 1.45;
  }

  pre.in {
    color: var(--green);
  }

  pre.err {
    color: #ff8b88;
  }

  pre.sys {
    color: var(--text-faint);
  }

  .prompt-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .prompt {
    color: var(--green);
    flex: 0 0 auto;
  }
</style>
