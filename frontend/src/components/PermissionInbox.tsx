import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CheckCircle2, Clock, ShieldAlert } from 'lucide-react'
import { listPendingPermissions, resolvePermission, type PendingPermission } from '../api'

interface Pending extends PendingPermission {
  received_at: number
}

interface QueuedAction {
  worker: string
  request_id: string
  behavior: 'allow' | 'deny'
  fires_at: number
  timer: ReturnType<typeof setTimeout>
}

const UNDO_MS = 3000

/** Permission Inbox with keyboard-first interaction and a 3-second per-row
 * undo window. Actions are queued (not sent to the hub) until the undo
 * window elapses. Multiple queued actions stack; Escape on the focused
 * toast cancels just that one. */
export function PermissionInbox({
  now,
  onCountChange,
}: {
  now: number
  /** Called whenever the rendered list size changes, so the parent can
   * keep its sidebar badge in sync with what the inbox is actually
   * showing — no more "badge says 4, inbox says 0" drift. */
  onCountChange?: (count: number) => void
}) {
  const [items, setItems] = useState<Pending[] | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Queued per request_id. Presence in this map = row is "pending-action"
  // visually but not yet flushed to the hub.
  const [queued, setQueued] = useState<Map<string, QueuedAction>>(new Map())
  const queuedRef = useRef(queued)
  queuedRef.current = queued

  const load = useCallback(async () => {
    try {
      const raw = await listPendingPermissions()
      setItems((prev) => {
        const prevMap = new Map((prev ?? []).map((p) => [p.request_id, p]))
        return raw.map((p) => ({
          ...p,
          received_at: prevMap.get(p.request_id)?.received_at ?? Date.now() / 1000,
        }))
      })
      onCountChange?.(raw.length)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [onCountChange])

  useEffect(() => { load() }, [load])

  // Live refresh — the App's global SSE subscription triggers re-renders
  // via workers state, but for the inbox we poll on focus+visibility for
  // safety in case events race. Cheap; 2s.
  useEffect(() => {
    const id = setInterval(load, 2000)
    return () => clearInterval(id)
  }, [load])

  // Cleanup timers on unmount so queued actions never fire against a
  // stale component.
  useEffect(() => {
    return () => {
      for (const q of queuedRef.current.values()) clearTimeout(q.timer)
    }
  }, [])

  const queueAction = useCallback((item: Pending, behavior: 'allow' | 'deny') => {
    const timer = setTimeout(async () => {
      try {
        await resolvePermission(item.worker, item.request_id, behavior)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setQueued((m) => {
          const next = new Map(m)
          next.delete(item.request_id)
          return next
        })
        load()
      }
    }, UNDO_MS)
    setQueued((m) => {
      const next = new Map(m)
      next.set(item.request_id, {
        worker: item.worker,
        request_id: item.request_id,
        behavior,
        fires_at: Date.now() / 1000 + UNDO_MS / 1000,
        timer,
      })
      return next
    })
  }, [load])

  const undo = useCallback((request_id: string) => {
    setQueued((m) => {
      const q = m.get(request_id)
      if (q) clearTimeout(q.timer)
      const next = new Map(m)
      next.delete(request_id)
      return next
    })
  }, [])

  // Advance focus to the next visible row after an action.
  const advance = useCallback(() => {
    if (!items || !focusId) return
    const idx = items.findIndex((p) => p.request_id === focusId)
    if (idx === -1) return
    const nextIdx = Math.min(idx + 1, items.length - 1)
    setFocusId(items[nextIdx]?.request_id ?? null)
  }, [items, focusId])

  // Keyboard handler scoped to the inbox list.
  const onKey = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (!items || items.length === 0) return
      const idx = items.findIndex((p) => p.request_id === focusId)
      if (e.key === 'ArrowDown' || e.key === 'j') {
        e.preventDefault()
        const nextIdx = idx === -1 ? 0 : Math.min(idx + 1, items.length - 1)
        setFocusId(items[nextIdx].request_id)
      } else if (e.key === 'ArrowUp' || e.key === 'k') {
        e.preventDefault()
        const nextIdx = idx === -1 ? items.length - 1 : Math.max(idx - 1, 0)
        setFocusId(items[nextIdx].request_id)
      } else if (focusId && (e.key === 'y' || e.key === 'Y')) {
        e.preventDefault()
        const item = items.find((p) => p.request_id === focusId)
        if (item && !queued.has(item.request_id)) { queueAction(item, 'allow'); advance() }
      } else if (focusId && (e.key === 'n' || e.key === 'N')) {
        e.preventDefault()
        const item = items.find((p) => p.request_id === focusId)
        if (item && !queued.has(item.request_id)) { queueAction(item, 'deny'); advance() }
      }
    },
    [items, focusId, queued, queueAction, advance],
  )

  const toasts = useMemo(() => Array.from(queued.values()), [queued])

  if (items === null) {
    return <div className="empty-state"><p className="title">Loading…</p></div>
  }

  if (items.length === 0) {
    return (
      <div className="empty-state">
        <CheckCircle2 size={32} aria-hidden="true" />
        <p className="title">All caught up</p>
        <p className="hint">No pending permissions across any worker.</p>
      </div>
    )
  }

  return (
    <>
      <div
        className="permission-list"
        role="log"
        aria-live="polite"
        aria-label="Pending permissions"
        tabIndex={0}
        onKeyDown={onKey}
      >
        {error && (
          <div className="permission-error" role="alert">{error}</div>
        )}
        {items.map((p) => {
          const age = Math.max(0, now - p.received_at)
          const overdue = age > 30
          const isQueued = queued.has(p.request_id)
          const isFocused = focusId === p.request_id
          return (
            <div
              key={p.request_id}
              className={[
                'permission-row',
                overdue ? 'overdue' : '',
                isFocused ? 'focused' : '',
                isQueued ? 'queued' : '',
              ].filter(Boolean).join(' ')}
              onClick={() => setFocusId(p.request_id)}
            >
              <div className="permission-row-main">
                <span className="worker">{p.worker}</span>
                <span className="tool">{p.tool_name}</span>
                {p.description && (
                  <span className="desc">{p.description}</span>
                )}
                <span className={`age${overdue ? ' overdue' : ''}`}>
                  {overdue && <Clock size={12} aria-hidden="true" />}
                  {overdue ? 'overdue · ' : ''}{formatAge(age)} ago
                </span>
              </div>
              {p.input_preview && (
                <pre className="permission-input-preview">{p.input_preview}</pre>
              )}
              <div className="permission-row-actions">
                <span className="chip">[Y] allow</span>
                <span className="chip">[N] deny</span>
              </div>
            </div>
          )
        })}
        <p className="permission-hint">
          <ShieldAlert size={12} aria-hidden="true" />
          Tab the list, then use ↑/↓ · Y / N — each action has a 3-second undo window.
        </p>
      </div>
      {toasts.length > 0 && (
        <div className="toast-stack" role="status" aria-live="polite">
          {toasts.map((t) => (
            <UndoToast key={t.request_id} action={t} now={now} onUndo={() => undo(t.request_id)} />
          ))}
        </div>
      )}
    </>
  )
}

function UndoToast({
  action,
  now,
  onUndo,
}: {
  action: QueuedAction
  now: number
  onUndo: () => void
}) {
  const remaining = Math.max(0, action.fires_at - now)
  return (
    <div className={`toast ${action.behavior}`}>
      <span>
        {action.behavior === 'allow' ? 'allowed' : 'denied'} · {action.worker}
      </span>
      <button type="button" onClick={onUndo}>
        Undo ({remaining.toFixed(0)}s)
      </button>
    </div>
  )
}

function formatAge(seconds: number): string {
  if (seconds < 1) return 'just now'
  if (seconds < 60) return `${Math.floor(seconds)}s`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h${(m % 60).toString().padStart(2, '0')}m`
}
