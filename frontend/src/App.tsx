import { lazy, Suspense, useEffect, useState, useCallback } from 'react'
import { Sidebar, type SidebarTab } from './components/Sidebar'
import {
  AuthError, clearBearer, getBearer, getHubSettings, listPendingPermissions,
  listWorkers, setBearer, setRecordingEnabled, subscribeEvents,
  tryDevBootstrap, updateHubSettings,
  type HubSettings, type Worker,
} from './api'
import { Roster } from './components/Roster'
import { PermissionInbox } from './components/PermissionInbox'
import { FlowEditor } from './components/FlowEditor'

// xterm.js is ~350KB gzipped. Defer it so the roster / inbox / flows
// pages don't pay for it. React.lazy splits it into its own chunk.
const TerminalPane = lazy(() =>
  import('./components/TerminalPane').then((m) => ({ default: m.TerminalPane })),
)

type View = 'workers' | 'permissions' | 'saved-messages' | 'settings'

function TerminalFallback() {
  return (
    <div className="empty-state">
      <p className="title">Loading terminal…</p>
    </div>
  )
}

export function App() {
  const [bearer, setBearerState] = useState<string | null>(getBearer())
  const [bootstrapping, setBootstrapping] = useState<boolean>(getBearer() === null)

  useEffect(() => {
    if (bearer !== null) return
    let cancelled = false
    tryDevBootstrap().then((token) => {
      if (cancelled) return
      if (token) {
        setBearer(token)
        setBearerState(token)
      }
      setBootstrapping(false)
    })
    return () => { cancelled = true }
  }, [bearer])

  if (bootstrapping) {
    return (
      <div className="empty-state" style={{ paddingTop: '4rem' }}>
        <p className="title">Connecting to TubeMail…</p>
      </div>
    )
  }
  if (!bearer) {
    return <AuthGate onAuthed={(t) => { setBearer(t); setBearerState(t) }} />
  }
  return <AuthedApp onSignOut={() => { clearBearer(); setBearerState(null) }} />
}

function AuthedApp({ onSignOut }: { onSignOut: () => void }) {
  // Pop-out mode: when URL has ?popout=1&worker=NAME, render the terminal
  // alone. Operator gets a near-native window, no chrome.
  const popOutWorker = getPopOutWorker()

  // URL-driven tab routing — matches iris-qa / leanspecs convention so
  // share-able links work and reload preserves the tab.
  const [view, setView] = useState<View>(() => urlTab() ?? 'workers')
  const [workers, setWorkers] = useState<Worker[] | null>(null)
  const [pendingFromInbox, setPendingFromInbox] = useState<number>(0)
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now() / 1000)
  const [openTerminalWorker, setOpenTerminalWorker] = useState<string | null>(null)

  const onTabChange = useCallback((tabId: string) => {
    const t = tabId as View
    setView(t)
    // Clicking the Workers tab from inside a terminal pane should snap
    // back to the roster — without this, view is already "workers" so
    // setView is a no-op and the terminal stays open. Same instinct as
    // the back button in the terminal chrome strip.
    if (t === 'workers') setOpenTerminalWorker(null)
    const p = new URLSearchParams(window.location.search)
    p.set('tab', t)
    window.history.replaceState({}, '', `?${p.toString()}`)
  }, [])

  const loadWorkers = useCallback(async () => {
    try {
      const ws = await listWorkers()
      setWorkers(ws)
      setError(null)
    } catch (e) {
      if (e instanceof AuthError) {
        onSignOut()
        return
      }
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [onSignOut])

  // Pending permissions from the SAME endpoint the inbox uses (online-only).
  // Avoids the badge / inbox count drift.
  const loadPendingCount = useCallback(async () => {
    try {
      const list = await listPendingPermissions()
      setPendingFromInbox(list.length)
    } catch {
      // Silent: if the endpoint fails we just don't show a stale badge.
    }
  }, [])

  useEffect(() => { loadWorkers() }, [loadWorkers])
  useEffect(() => { loadPendingCount() }, [loadPendingCount])

  // Shared clock ticker for state-badge counters.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(id)
  }, [])

  // Subscribe to hub-wide SSE: refetch roster + pending count on any event.
  useEffect(() => {
    const close = subscribeEvents(
      () => { loadWorkers(); loadPendingCount() },
      (e) => {
        if (e instanceof AuthError) onSignOut()
        else setError(e.message)
      },
    )
    return close
  }, [loadWorkers, loadPendingCount, onSignOut])

  // Pop-out terminal — no sidebar. The `popout-host` class makes the
  // wrapper a 100vh flex column so the inner TerminalPane can grow to
  // fill the actual window. Without this, .terminal-pane's fixed
  // calc(100vh - 140px) leaves 140px of dead space at the bottom and
  // the user has to manually grow the window before the prompt
  // appears.
  if (popOutWorker) {
    const w = workers?.find((x) => x.name === popOutWorker)
    if (!w) {
      return (
        <div className="empty-state" style={{ padding: '4rem 1rem' }}>
          <p className="title">Waiting for worker {popOutWorker}…</p>
          <p className="hint">Close this window if the worker is gone.</p>
        </div>
      )
    }
    return (
      <div className="popout-host">
        <Suspense fallback={<TerminalFallback />}>
          <TerminalPane worker={w} now={now} onClose={() => window.close()} />
        </Suspense>
      </div>
    )
  }

  const terminalWorker = openTerminalWorker
    ? workers?.find((w) => w.name === openTerminalWorker) ?? null
    : null

  const tabs: SidebarTab[] = [
    { id: 'workers', label: 'Workers' },
    { id: 'permissions', label: 'Permissions', badge: pendingFromInbox },
    { id: 'saved-messages', label: 'Saved Messages' },
  ]
  const bottomTabs: SidebarTab[] = [{ id: 'settings', label: 'Settings' }]

  return (
    <div className="app-layout">
      <Sidebar
        title="tubemail"
        tabs={tabs}
        bottomTabs={bottomTabs}
        activeTab={view}
        onTabChange={onTabChange}
      />
      <main className="main-content">
        <div className="header-strip">
          <span className="title">{titleFor(view)}</span>
          <span>·</span>
          <span>{subtitleFor(view, workers, pendingFromInbox)}</span>
          {error && (
            <>
              <span>·</span>
              <span style={{ color: 'var(--danger)' }}>{error}</span>
            </>
          )}
        </div>
        {view === 'workers' && (
          terminalWorker ? (
            <Suspense fallback={<TerminalFallback />}>
              <TerminalPane
                worker={terminalWorker}
                now={now}
                onClose={() => setOpenTerminalWorker(null)}
              />
            </Suspense>
          ) : workers === null ? (
            <div className="empty-state"><p className="title">Loading…</p></div>
          ) : (
            <Roster
              workers={workers}
              now={now}
              onSelect={(w) => setOpenTerminalWorker(w.name)}
              onPurge={async (name) => {
                if (!confirm(`Permanently remove ${name} from the registry?`)) return
                await fetch(`/api/workers/${encodeURIComponent(name)}`, {
                  method: 'DELETE',
                  headers: {
                    Authorization: `Bearer ${getBearer() ?? ''}`,
                  },
                })
                loadWorkers()
              }}
              onToggleRecording={async (name, enabled) => {
                await setRecordingEnabled(name, enabled)
                loadWorkers()
              }}
            />
          )
        )}
        {view === 'permissions' && (
          <PermissionInbox
            now={now}
            onCountChange={(n) => setPendingFromInbox(n)}
          />
        )}
        {view === 'saved-messages' && <FlowEditor workers={workers ?? []} />}
        {view === 'settings' && <SettingsView onSignOut={onSignOut} />}
      </main>
    </div>
  )
}

function SettingsView({ onSignOut }: { onSignOut: () => void }) {
  const [settings, setSettings] = useState<HubSettings | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getHubSettings()
      .then((s) => { if (!cancelled) setSettings(s) })
      .catch((e) => {
        if (cancelled) return
        if (e instanceof AuthError) onSignOut()
        else setLoadError(e instanceof Error ? e.message : String(e))
      })
    return () => { cancelled = true }
  }, [onSignOut])

  const apply = async (patch: Partial<HubSettings>) => {
    setSaving(true)
    setSaveError(null)
    try {
      const next = await updateHubSettings(patch)
      setSettings(next)
    } catch (e) {
      if (e instanceof AuthError) onSignOut()
      else setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card" style={{ maxWidth: 640 }}>
      <h2>Settings</h2>

      <h3 style={{ marginTop: '1.25rem' }}>Recording</h3>
      <p className="hint">
        When recording is on, every byte of a worker's terminal output is
        teed to a file pair on disk: an asciinema <code>.cast</code> for
        replay, and a <code>.frames.jsonl</code> for grep / time slicing.
        Files rotate by size; oldest are GC'd per worker.
      </p>
      {loadError && <p className="error">{loadError}</p>}
      {settings && (
        <>
          <label
            style={{
              display: 'flex', alignItems: 'center', gap: '0.5rem',
              marginTop: '0.75rem', cursor: 'pointer',
            }}
          >
            <input
              type="checkbox"
              checked={settings.recording_enabled_by_default}
              disabled={saving}
              onChange={(e) =>
                apply({ recording_enabled_by_default: e.target.checked })
              }
            />
            <span>Record new workers by default</span>
          </label>
          <p className="hint" style={{ marginTop: '0.25rem' }}>
            Existing workers keep their per-worker setting. Toggle individual
            workers from the Workers tab.
          </p>

          <div style={{ display: 'flex', gap: '1rem', marginTop: '1rem' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
              <span className="hint">Max file size (MiB)</span>
              <input
                type="number"
                min={1}
                max={1024}
                step={1}
                disabled={saving}
                value={Math.round(settings.recording_max_bytes_per_file / (1024 * 1024))}
                onChange={(e) => {
                  const mib = Math.max(1, Number(e.target.value) || 1)
                  apply({ recording_max_bytes_per_file: mib * 1024 * 1024 })
                }}
                style={{ width: '6rem' }}
              />
            </label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
              <span className="hint">Files kept per worker</span>
              <input
                type="number"
                min={1}
                max={64}
                step={1}
                disabled={saving}
                value={settings.recording_keep_files}
                onChange={(e) => {
                  const n = Math.max(1, Math.min(64, Number(e.target.value) || 1))
                  apply({ recording_keep_files: n })
                }}
                style={{ width: '6rem' }}
              />
            </label>
          </div>
          {saveError && <p className="error" style={{ marginTop: '0.5rem' }}>{saveError}</p>}
        </>
      )}

      <h3 style={{ marginTop: '1.25rem' }}>Session</h3>
      <p className="hint">
        Your bearer token is stored in this browser's localStorage for
        the current origin. Clearing it sends you back to the auth gate
        on next reload.
      </p>
      <button
        onClick={onSignOut}
        style={{
          background: 'var(--gray-200)', border: 0, padding: '0.375rem 0.75rem',
          borderRadius: 'var(--radius)', cursor: 'pointer', fontSize: '0.8125rem',
          marginTop: '0.5rem',
        }}
      >
        Sign out (clear bearer)
      </button>
    </div>
  )
}

function urlTab(): View | null {
  if (typeof window === 'undefined') return null
  const t = new URLSearchParams(window.location.search).get('tab')
  if (t === 'workers' || t === 'permissions' || t === 'saved-messages' || t === 'settings') {
    return t
  }
  return null
}

function getPopOutWorker(): string | null {
  if (typeof window === 'undefined') return null
  const params = new URLSearchParams(window.location.search)
  if (params.get('popout') !== '1') return null
  return params.get('worker') || null
}

function titleFor(view: View): string {
  if (view === 'workers') return 'Workers'
  if (view === 'permissions') return 'Permissions'
  if (view === 'saved-messages') return 'Saved Messages'
  return 'Settings'
}

function subtitleFor(view: View, workers: Worker[] | null, pendingCount: number): string {
  if (view === 'workers') {
    const total = workers?.length ?? 0
    const online = workers?.filter((w) => w.online).length ?? 0
    return `${online} online · ${total} known`
  }
  if (view === 'permissions') return `${pendingCount} pending`
  if (view === 'saved-messages') return 'named work-order templates'
  return ''
}

function AuthGate({ onAuthed }: { onAuthed: (token: string) => void }) {
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!value.trim()) return
    const res = await fetch('/api/workers', {
      headers: { Authorization: `Bearer ${value.trim()}` },
    })
    if (res.ok) {
      onAuthed(value.trim())
    } else if (res.status === 401) {
      setError('That token didn\'t work — double-check the value in .env.')
    } else {
      setError(`${res.status} ${res.statusText}`)
    }
  }

  return (
    <form className="auth-gate" onSubmit={submit}>
      <h2>Connect to TubeMail</h2>
      <p>
        TubeMail is a transport hub for Claude Code workers. The hub is
        running on <code>{typeof window !== 'undefined' ? window.location.host : ''}</code>,
        but the browser session needs a token to talk to it.
      </p>
      <p>
        On the machine running the hub, the token lives in <code>.env</code>
        as <code>TUBEMAIL_SECRET</code>. To grab it:
      </p>
      <pre className="auth-gate-cmd">grep TUBEMAIL_SECRET ~/PycharmProjects/ai-agents/tubemail/.env</pre>
      <p className="auth-gate-hint">
        Paste the value (everything after the <code>=</code>) here. It's
        saved in your browser's localStorage for this origin only — no
        cookies, no third parties.
      </p>
      <input
        type="password"
        placeholder="paste TUBEMAIL_SECRET value"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        autoFocus
        autoComplete="off"
        spellCheck={false}
      />
      <button type="submit" disabled={!value.trim()}>Connect</button>
      {error && <div className="error">{error}</div>}
      <details className="auth-gate-help">
        <summary>Why am I seeing this?</summary>
        <p>
          You're connecting from a different machine than the hub, OR the
          hub was started with <code>TUBEMAIL_DISABLE_DEV_BOOTSTRAP=1</code>.
          If you're on the same machine as the hub, restart your browser
          session and the connection should be automatic — no token to paste.
        </p>
      </details>
    </form>
  )
}
