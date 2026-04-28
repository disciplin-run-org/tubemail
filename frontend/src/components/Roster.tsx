import { ArrowDown, ArrowUp, ArrowUpDown, Circle, CircleDot, FolderOpen, RotateCw, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { Worker } from '../api'
import { StateBadge, uiState } from './StateBadge'

type SortCol = 'name' | 'mgr' | 'version' | 'state' | 'cwd' | 'ctx' | 'rec'
type SortDir = 'asc' | 'desc'
interface SortState {
  col: SortCol | null
  dir: SortDir
}

const STATE_ORDER: Record<string, number> = {
  busy: 0,
  waiting: 1,
  idle: 2,
  unknown: 3,
  offline: 4,
  'offline-clean': 5,
}

export interface RosterProps {
  workers: Worker[]
  now: number
  onSelect?: (worker: Worker) => void
  /** Called when the user clicks the trash icon on a row. Caller is
   * expected to confirm + DELETE /api/workers/{name} + refresh. */
  onPurge?: (name: string) => void
  /** Called when the user clicks the recording toggle. Caller hits
   * POST /api/workers/{name}/recording with `{enabled}` and refreshes
   * the roster so the new state propagates back into props. */
  onToggleRecording?: (name: string, enabled: boolean) => Promise<void>
  /** Called when the user clicks the restart icon on an online row.
   * Caller is expected to confirm + POST
   * /api/workers/{name}/update-manager + refresh. The Claude session
   * is preserved (--continue); only the python manager wrapper is
   * re-execed. */
  onRestartManager?: (name: string) => Promise<void>
}

/** Worker roster grouped by project — matches the `tm_list_workers` MCP
 * tool layout. Click a column header to sort by that column; sorted
 * mode flattens rows (no grouping). */
export function Roster({ workers, now, onSelect, onPurge, onToggleRecording, onRestartManager }: RosterProps) {
  const [sort, setSort] = useState<SortState>({ col: null, dir: 'asc' })

  // Click cycles: idle → asc → desc → idle.
  const cycle = (col: SortCol) => setSort((prev) => {
    if (prev.col !== col) return { col, dir: 'asc' }
    if (prev.dir === 'asc') return { col, dir: 'desc' }
    return { col: null, dir: 'asc' }
  })

  // Manager lookup, same pattern tm_list_workers uses (workers.py:117).
  const managerOnline = useMemo(() => {
    const m = new Map<string, boolean>()
    for (const w of workers) {
      if (w.name.endsWith('-manager')) {
        m.set(w.name.slice(0, -'-manager'.length), w.online)
      }
    }
    return m
  }, [workers])

  const managerVersion = useMemo(() => {
    const m = new Map<string, string>()
    for (const w of workers) {
      if (w.name.endsWith('-manager')) {
        m.set(w.name.slice(0, -'-manager'.length), w.forwarder_version || '')
      }
    }
    return m
  }, [workers])

  // Drop manager entries from the row list — they render inline as the
  // mgr column on their owner.
  const realWorkers = useMemo(
    () => workers.filter((w) => !w.name.endsWith('-manager')),
    [workers],
  )

  const sortedFlat = useMemo(
    () => sort.col === null ? null : sortWorkers(realWorkers, sort, managerOnline, managerVersion),
    [realWorkers, sort, managerOnline, managerVersion],
  )

  if (realWorkers.length === 0) {
    return (
      <div className="empty-state">
        <p className="title">No workers connected</p>
        <p className="hint">
          Launch one from any project directory: <code>claude-tm</code>
        </p>
      </div>
    )
  }

  const groups = groupByProject(realWorkers)

  return (
    <div className="roster-table">
      <div className="roster-header-row">
        <span></span>
        <SortHeader col="name" label="Worker" sort={sort} onClick={cycle} />
        <SortHeader col="mgr" label="Mgr" sort={sort} onClick={cycle} />
        <SortHeader col="version" label="Version" sort={sort} onClick={cycle} />
        <SortHeader col="state" label="State" sort={sort} onClick={cycle} />
        <SortHeader col="cwd" label="Directory" sort={sort} onClick={cycle} />
        <SortHeader col="ctx" label="Ctx" sort={sort} onClick={cycle} align="right" title="Context window %" />
        <SortHeader col="rec" label="Rec" sort={sort} onClick={cycle} title="Recording" />
        <span></span>
      </div>
      {sortedFlat ? (
        sortedFlat.map((w) => (
          <RosterRow
            key={w.name}
            worker={w}
            managerOnline={managerOnline}
            managerVersion={managerVersion}
            now={now}
            onSelect={onSelect}
            onPurge={onPurge}
            onToggleRecording={onToggleRecording}
            onRestartManager={onRestartManager}
            indent={false}
          />
        ))
      ) : (
        Array.from(groups.entries()).map(([project, members]) => (
          <ProjectGroup
            key={project}
            project={project}
            members={members}
            managerOnline={managerOnline}
            managerVersion={managerVersion}
            now={now}
            onSelect={onSelect}
            onPurge={onPurge}
            onToggleRecording={onToggleRecording}
            onRestartManager={onRestartManager}
          />
        ))
      )}
    </div>
  )
}

function SortHeader({
  col, label, sort, onClick, align, title,
}: {
  col: SortCol
  label: string
  sort: SortState
  onClick: (col: SortCol) => void
  align?: 'right'
  title?: string
}) {
  const active = sort.col === col
  const Icon = active ? (sort.dir === 'asc' ? ArrowUp : ArrowDown) : ArrowUpDown
  return (
    <span
      className={`roster-sort-header${active ? ' active' : ''}`}
      onClick={() => onClick(col)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onClick(col) }}
      title={title}
      style={align === 'right' ? { justifyContent: 'flex-end' } : undefined}
    >
      <span>{label}</span>
      <Icon size={11} aria-hidden="true" />
    </span>
  )
}

function sortWorkers(
  workers: Worker[],
  sort: SortState,
  managerOnline: Map<string, boolean>,
  managerVersion: Map<string, string>,
): Worker[] {
  const dir = sort.dir === 'asc' ? 1 : -1
  const cmp = (a: Worker, b: Worker): number => {
    switch (sort.col) {
      case 'name': return dir * a.name.localeCompare(b.name)
      case 'mgr': {
        // Manager up first (true > false in this ordering = top of asc).
        const av = managerOnline.get(a.name) ? 0 : 1
        const bv = managerOnline.get(b.name) ? 0 : 1
        return dir * (av - bv) || a.name.localeCompare(b.name)
      }
      case 'version': {
        const av = managerVersion.get(a.name) || a.forwarder_version || ''
        const bv = managerVersion.get(b.name) || b.forwarder_version || ''
        return dir * av.localeCompare(bv) || a.name.localeCompare(b.name)
      }
      case 'state': {
        const av = STATE_ORDER[uiState(a)] ?? 99
        const bv = STATE_ORDER[uiState(b)] ?? 99
        return dir * (av - bv) || a.name.localeCompare(b.name)
      }
      case 'cwd': return dir * a.cwd.localeCompare(b.cwd) || a.name.localeCompare(b.name)
      case 'ctx': {
        // Nulls always sort last regardless of direction — workers
        // without a context value are noise, not a sort target.
        const av = a.context_pct
        const bv = b.context_pct
        if (av === null && bv === null) return a.name.localeCompare(b.name)
        if (av === null) return 1
        if (bv === null) return -1
        return dir * (av - bv) || a.name.localeCompare(b.name)
      }
      case 'rec': {
        const av = a.recording_enabled ? 0 : 1
        const bv = b.recording_enabled ? 0 : 1
        return dir * (av - bv) || a.name.localeCompare(b.name)
      }
      default: return 0
    }
  }
  return [...workers].sort(cmp)
}

function ProjectGroup({
  project,
  members,
  managerOnline,
  managerVersion,
  now,
  onSelect,
  onPurge,
  onToggleRecording,
  onRestartManager,
}: {
  project: string
  members: Worker[]
  managerOnline: Map<string, boolean>
  managerVersion: Map<string, string>
  now: number
  onSelect?: (w: Worker) => void
  onPurge?: (name: string) => void
  onToggleRecording?: (name: string, enabled: boolean) => Promise<void>
  onRestartManager?: (name: string) => Promise<void>
}) {
  // Single-member groups render as a flat row, no header (cleaner).
  if (members.length === 1) {
    return (
      <RosterRow
        worker={members[0]}
        managerOnline={managerOnline}
        managerVersion={managerVersion}
        now={now}
        onSelect={onSelect}
        onPurge={onPurge}
        onToggleRecording={onToggleRecording}
        onRestartManager={onRestartManager}
        indent={false}
      />
    )
  }
  return (
    <>
      <div className="roster-project-header">
        {/* Folder glyph signals "this is a parent group". The project's
            status is implicit in its children's badges, so no duplicate
            green/red indicator here. */}
        <span className="proj-icon" aria-hidden="true">
          <FolderOpen size={14} />
        </span>
        <span className="proj-name">{project}</span>
        <span></span>
        <span></span>
        <span></span>
        <span className="proj-roles">{members.length} sessions</span>
        <span></span>
        <span></span>
        <span></span>
      </div>
      {members.map((w) => (
        <RosterRow
          key={w.name}
          worker={w}
          managerOnline={managerOnline}
          managerVersion={managerVersion}
          now={now}
          onSelect={onSelect}
          onPurge={onPurge}
          onToggleRecording={onToggleRecording}
          onRestartManager={onRestartManager}
          indent={true}
        />
      ))}
    </>
  )
}

function RosterRow({
  worker,
  managerOnline,
  managerVersion,
  now,
  onSelect,
  onPurge,
  onToggleRecording,
  onRestartManager,
  indent,
}: {
  worker: Worker
  managerOnline: Map<string, boolean>
  managerVersion: Map<string, string>
  now: number
  onSelect?: (w: Worker) => void
  onPurge?: (name: string) => void
  onToggleRecording?: (name: string, enabled: boolean) => Promise<void>
  onRestartManager?: (name: string) => Promise<void>
  indent: boolean
}) {
  const state = uiState(worker)
  const busySince = state === 'busy' ? worker.last_activity : undefined
  const mgrUp = managerOnline.get(worker.name)
  const mgrLabel = mgrUp === undefined ? '—' : mgrUp ? '🟢' : '🔴'
  const version = managerVersion.get(worker.name) || worker.forwarder_version || '—'
  const cwd = (worker.cwd || '').replace(/^\/home\/[^/]+\/PycharmProjects\/ai-agents\//, '')

  // Optimistic toggle: flip immediately on click, then let the props refresh
  // confirm. If the API call fails, revert. Stays predictable through the
  // SSE re-fetch latency window so users don't double-click thinking it
  // didn't register.
  const [pending, setPending] = useState(false)
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const recOn = optimistic ?? worker.recording_enabled
  const RecIcon = recOn ? CircleDot : Circle

  const handleToggle = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!onToggleRecording || pending) return
    const next = !recOn
    setPending(true)
    setOptimistic(next)
    try {
      await onToggleRecording(worker.name, next)
    } catch {
      setOptimistic(!next)
    } finally {
      setPending(false)
      // Once props update with the server's authoritative value, drop our
      // local override so the row reflects reality on the next render.
      setTimeout(() => setOptimistic(null), 0)
    }
  }

  return (
    <div
      className={`roster-row${indent ? ' indented' : ''}`}
      onClick={() => onSelect?.(worker)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onSelect?.(worker) }}
    >
      {/* Col 1 left as spacer — keeps alignment with the project-group
          folder icon column. State / connectivity info is shown via the
          State badge and Mgr columns; the old SSE dot was redundant. */}
      <span aria-hidden="true" />
      <span className="name">{worker.name}</span>
      <span className="mgr">{mgrLabel}</span>
      <span className="version">{version}</span>
      <StateBadge state={state} busySince={busySince} now={now} />
      <span className="cwd" title={worker.cwd}>{cwd || '—'}</span>
      <span
        className="ctx"
        title={
          worker.context_pct === null
            ? 'context % not yet reported by manager'
            : `context window: ${worker.context_pct}%`
        }
      >
        {worker.context_pct === null ? '—' : `${worker.context_pct}%`}
      </span>
      {onToggleRecording ? (
        <button
          className={`row-action row-action-rec${recOn ? ' on' : ''}`}
          title={recOn ? 'Recording — click to stop' : 'Not recording — click to start'}
          aria-pressed={recOn}
          disabled={pending}
          onClick={handleToggle}
        >
          <RecIcon size={14} />
        </button>
      ) : <span></span>}
      {/* Action column: trash for offline rows, restart-manager for online
          rows whose manager is up. Mutex by state — at most one button
          appears per row, so the column stays a fixed width. */}
      {onPurge && !worker.online ? (
        <button
          className="row-action"
          title="Permanently remove from registry"
          onClick={(e) => {
            e.stopPropagation()
            onPurge(worker.name)
          }}
        >
          <Trash2 size={14} />
        </button>
      ) : onRestartManager && worker.online && mgrUp ? (
        <RestartManagerButton
          worker={worker.name}
          onRestart={onRestartManager}
        />
      ) : (
        <span></span>
      )}
    </div>
  )
}

function RestartManagerButton({
  worker,
  onRestart,
}: {
  worker: string
  onRestart: (name: string) => Promise<void>
}) {
  const [pending, setPending] = useState(false)
  const handleClick = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (pending) return
    setPending(true)
    try {
      await onRestart(worker)
    } finally {
      setPending(false)
    }
  }
  return (
    <button
      className="row-action"
      title="Restart manager (re-exec the python wrapper; Claude session preserved via --continue)"
      disabled={pending}
      onClick={handleClick}
    >
      <RotateCw size={14} className={pending ? 'spin' : undefined} />
    </button>
  )
}

function projectOf(name: string): string {
  // Strip `-<role>-tm` first, then `-tm`. Workers like `iris-qa-tm` have
  // no role so they fall through to the last-segment heuristic.
  const m = name.match(/^(.+?)(?:-[a-z]+)?-tm$/)
  return m ? m[1] : name
}

function groupByProject(workers: Worker[]): Map<string, Worker[]> {
  const groups = new Map<string, Worker[]>()
  for (const w of workers) {
    const key = projectOf(w.name)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(w)
  }
  for (const members of groups.values()) {
    members.sort((a, b) => a.name.localeCompare(b.name))
  }
  return new Map(Array.from(groups.entries()).sort((a, b) => a[0].localeCompare(b[0])))
}
