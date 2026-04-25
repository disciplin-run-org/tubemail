import { useCallback, useEffect, useMemo, useState } from 'react'
import { Bookmark, Plus, Play, Save, Trash2 } from 'lucide-react'
import {
  deleteFlow,
  getRunLog,
  listFlows,
  runFlow,
  saveFlow,
  type Flow,
  type RunLog,
  type Worker,
} from '../api'

interface FlowEditorProps {
  workers: Worker[]
}

interface Draft {
  name: string
  body: string
  default_worker: string
  // true = loaded from hub (editing existing); false = new
  existed: boolean
}

export function FlowEditor({ workers }: FlowEditorProps) {
  const [flows, setFlows] = useState<Flow[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [log, setLog] = useState<RunLog | null>(null)

  const load = useCallback(async () => {
    try {
      const fs = await listFlows()
      setFlows(fs)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => { load() }, [load])

  const selectFlow = useCallback((f: Flow | null) => {
    if (f === null) {
      setSelected(null)
      setDraft({ name: '', body: '', default_worker: '', existed: false })
      setLog(null)
      return
    }
    setSelected(f.name)
    setDraft({
      name: f.name,
      body: f.body,
      default_worker: f.default_worker ?? '',
      existed: true,
    })
    setLog(null)
  }, [])

  const onSave = useCallback(async () => {
    if (!draft || !draft.name.trim() || !draft.body.trim()) return
    setSaving(true)
    try {
      const res = await saveFlow(
        draft.name.trim(),
        draft.body,
        draft.default_worker.trim() || null,
      )
      if (!res.ok) {
        setError(res.error ?? 'save failed')
      } else {
        await load()
        setSelected(draft.name.trim())
        setDraft({ ...draft, existed: true })
      }
    } finally {
      setSaving(false)
    }
  }, [draft, load])

  const onDelete = useCallback(async () => {
    if (!selected) return
    if (!confirm(`Delete flow "${selected}"? Past run logs are kept.`)) return
    await deleteFlow(selected)
    setSelected(null)
    setDraft(null)
    await load()
  }, [selected, load])

  const onRun = useCallback(async () => {
    if (!draft?.existed) {
      setError('save the flow before running')
      return
    }
    const res = await runFlow(
      draft.name,
      draft.default_worker.trim() || undefined,
    )
    if (!res.ok || !res.run_id) {
      setError(res.error ?? 'run failed')
      return
    }
    // Poll the run log until a reply comes back or the user navigates away.
    const runId = res.run_id
    const started = Date.now()
    const poll = async () => {
      const r = await getRunLog(runId)
      if (r.ok && r.run) setLog(r.run)
      // Stop polling after 10 min or when the run has a trailing outbound event
      if (Date.now() - started > 10 * 60 * 1000) return
      const hasReply = r.run?.events.some((e) => e.kind === 'outbound') ?? false
      if (!hasReply) setTimeout(poll, 2000)
    }
    poll()
  }, [draft])

  const selectedFlow = useMemo(
    () => flows?.find((f) => f.name === selected) ?? null,
    [flows, selected],
  )

  return (
    <div className="flow-editor">
      {/* Left pane — flow list */}
      <aside className="flow-list">
        <div className="flow-list-header">
          <span>Flows</span>
          <button
            type="button"
            className="icon-btn"
            title="New flow"
            onClick={() => selectFlow(null)}
          >
            <Plus size={14} />
          </button>
        </div>
        {flows === null ? (
          <p className="flow-list-empty">Loading…</p>
        ) : flows.length === 0 ? (
          <p className="flow-list-empty">
            No saved messages yet. Click <strong>+</strong> to create one.
          </p>
        ) : (
          <ul>
            {flows.map((f) => (
              <li
                key={f.name}
                className={selected === f.name ? 'active' : ''}
                onClick={() => selectFlow(f)}
              >
                <span className="flow-name">{f.name}</span>
                {f.last_run_at ? (
                  <span className="flow-last-run">{formatRel(f.last_run_at)}</span>
                ) : (
                  <span className="flow-last-run">never run</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </aside>

      {/* Right pane — editor */}
      <div className="flow-editor-pane">
        {draft === null ? (
          <div className="empty-state">
            <Bookmark size={32} aria-hidden="true" />
            <p className="title">Saved Messages</p>
            <p className="hint" style={{ maxWidth: 480, margin: '0.75rem auto' }}>
              Saved messages are reusable work-order templates. Type a
              long instruction once (e.g. <em>"run iris_qa_run on
              behavior 4.1.1 and classify failures"</em>), save it with a
              short name, and replay it any time against any worker —
              from this UI or programmatically via the
              {' '}<code>tm_run_flow</code> MCP tool. The forthcoming
              Quartermaster orchestrator drives flows automatically.
            </p>
            <p className="hint">
              Click <strong>+</strong> in the left pane to create your first
              one.
            </p>
          </div>
        ) : (
          <>
            <div className="flow-editor-form">
              <label>
                <span>Name</span>
                <input
                  type="text"
                  value={draft.name}
                  placeholder="e.g. iris-qa-run-3.5.8"
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  disabled={draft.existed}
                  autoFocus={!draft.existed}
                />
                {draft.existed && (
                  <span className="hint">rename isn't supported in v1 — delete + recreate</span>
                )}
              </label>

              <label>
                <span>Default target worker</span>
                <select
                  value={draft.default_worker}
                  onChange={(e) => setDraft({ ...draft, default_worker: e.target.value })}
                >
                  <option value="">(choose at run time)</option>
                  {workers.map((w) => (
                    <option key={w.name} value={w.name}>
                      {w.name} · {w.state}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                <span>Body</span>
                <textarea
                  value={draft.body}
                  rows={12}
                  placeholder="What do you want the worker to do?"
                  onChange={(e) => setDraft({ ...draft, body: e.target.value })}
                />
              </label>

              <div className="flow-editor-actions">
                <button
                  type="button"
                  className="btn-primary"
                  disabled={saving || !draft.name.trim() || !draft.body.trim()}
                  onClick={onSave}
                >
                  <Save size={14} /> {saving ? 'Saving…' : 'Save'}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={!draft.existed}
                  onClick={onRun}
                  title={draft.existed ? 'Run this flow' : 'Save before running'}
                >
                  <Play size={14} /> Run now
                </button>
                {draft.existed && selectedFlow && (
                  <button
                    type="button"
                    className="btn-danger"
                    onClick={onDelete}
                  >
                    <Trash2 size={14} /> Delete
                  </button>
                )}
              </div>

              {error && <div className="permission-error" role="alert">{error}</div>}
            </div>

            {log && (
              <div className="run-log">
                <div className="run-log-header">
                  Run log · {log.run_id}
                  {log.finished_at && <span className="finished"> · finished</span>}
                </div>
                <div className="run-log-events">
                  {log.events.length === 0 ? (
                    <p className="flow-list-empty">Waiting for worker reply…</p>
                  ) : (
                    log.events.map((e) => (
                      <div key={e.event_id} className={`run-event kind-${e.kind}`}>
                        <span className="kind">{e.kind}</span>
                        <span className="ts">{new Date(e.ts * 1000).toLocaleTimeString()}</span>
                        <pre>{e.content}</pre>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function formatRel(ts: number): string {
  const secs = Math.max(0, Date.now() / 1000 - ts)
  if (secs < 60) return `${Math.floor(secs)}s ago`
  const m = Math.floor(secs / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}
