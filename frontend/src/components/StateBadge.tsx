import { CircleCheck, CircleDashed, CircleHelp, CircleX, Loader2, ShieldAlert } from 'lucide-react'
import type { Worker } from '../api'

type UiState =
  | 'idle'
  | 'busy'
  | 'waiting'
  | 'offline'
  | 'offline-clean'
  | 'unknown'

/** Compute the UI state badge for a worker. Combines the hub-reported
 * `state` with the locally-computed `offline` distinction (SSE-connected
 * vs not, clean-exit vs crashed). */
export function uiState(w: Worker): UiState {
  if (!w.online && w.exited_cleanly) return 'offline-clean'
  if (!w.online) return 'offline'
  if (w.state === 'waiting_permission') return 'waiting'
  if (w.state === 'busy') return 'busy'
  if (w.state === 'idle') return 'idle'
  return 'unknown'
}

export interface StateBadgeProps {
  state: UiState
  busySince?: number  // epoch seconds of last inbound, for counter
  now?: number         // epoch seconds, current wall clock (shared ticker)
}

export function StateBadge({ state, busySince, now }: StateBadgeProps) {
  const label = labelFor(state, busySince, now)
  const Icon = iconFor(state)
  return (
    <span className={`state-badge ${state}`} aria-label={label}>
      <Icon aria-hidden="true" />
      <span>{label}</span>
    </span>
  )
}

function labelFor(state: UiState, busySince?: number, now?: number): string {
  if (state === 'busy' && busySince && now) {
    return `busy · ${formatElapsed(now - busySince)}`
  }
  switch (state) {
    case 'idle': return 'idle'
    case 'busy': return 'busy'
    case 'waiting': return 'waiting'
    case 'offline': return 'offline'
    case 'offline-clean': return 'exited'
    case 'unknown': return 'unknown'
  }
}

function iconFor(state: UiState) {
  switch (state) {
    case 'idle': return CircleCheck
    case 'busy': return Loader2
    case 'waiting': return ShieldAlert
    case 'offline': return CircleX
    case 'offline-clean': return CircleDashed
    case 'unknown': return CircleHelp
  }
}

function formatElapsed(seconds: number): string {
  if (seconds < 0) return '0s'
  if (seconds < 60) return `${Math.floor(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  if (m < 60) return `${m}m${s.toString().padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h${(m % 60).toString().padStart(2, '0')}m`
}
