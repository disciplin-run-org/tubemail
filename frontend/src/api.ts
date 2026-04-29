/** Frontend API client. Thin wrapper over fetch + SSE that carries the
 * bearer token the operator pastes into the auth gate.
 *
 * The bearer lives in localStorage for the session. In v2 we'll swap this
 * for the ticket-exchange pattern so the bearer never leaves the initial
 * handshake. */

const STORAGE_KEY = 'tubemail.bearer'

export function getBearer(): string | null {
  return localStorage.getItem(STORAGE_KEY)
}

export function setBearer(token: string): void {
  localStorage.setItem(STORAGE_KEY, token)
}

export function clearBearer(): void {
  localStorage.removeItem(STORAGE_KEY)
}

/** First-load helper: ask the hub for the operator's bearer over loopback.
 * Returns the token if the hub serves it (same machine, dev mode), or
 * `null` if the user needs to paste it manually (remote browser, prod
 * deploy, or operator opted out). Never throws — failure means "show the
 * auth gate." */
export async function tryDevBootstrap(): Promise<string | null> {
  try {
    const res = await fetch('/api/dev-bootstrap', { credentials: 'omit' })
    if (!res.ok) return null
    const body = await res.json()
    if (typeof body?.bearer === 'string' && body.bearer) return body.bearer
    return null
  } catch {
    return null
  }
}

function authHeaders(): Record<string, string> {
  const token = getBearer()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { ...authHeaders(), ...(init?.headers || {}) },
  })
  if (res.status === 401) {
    throw new AuthError('invalid or missing bearer')
  }
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`)
  }
  return res.json()
}

export class AuthError extends Error {
  constructor(msg: string) {
    super(msg)
    this.name = 'AuthError'
  }
}

/* Hub-reported shape from tm_list_workers / /api/workers. */
export interface Worker {
  name: string
  cwd: string
  state: 'idle' | 'busy' | 'waiting_permission' | 'unknown'
  pending_count: number
  last_activity: number
  registered_at: number
  forwarder_version: string
  online: boolean
  exited_cleanly: boolean
  recording_enabled: boolean
  /** Last-known context-window % parsed from the worker's TUI status bar.
   * null when the manager hasn't posted a value yet (older managers
   * stay null forever, which the UI renders as "—"). */
  context_pct: number | null
}

export interface WorkersResponse {
  workers: Worker[]
}

export interface HubHealth {
  status: string
  service: string
  version: string
  /** Short (7-char) git SHA of the source the hub is running against,
   * or '' when the gitdir bind-mount is missing / not a submodule.
   * The Roster compares each manager's reported SHA against this to
   * decide stale-vs-current — the canonical reference, immune to the
   * "majority is stale" and "only-one-restarted" edge cases of the
   * client-side heuristic. */
  git_sha?: string
}

/** /health is unauthenticated by design (Docker healthchecks etc.).
 * Doesn't go through fetchJSON because it doesn't need the bearer. */
export async function getHubHealth(): Promise<HubHealth> {
  const res = await fetch('/health')
  if (!res.ok) throw new Error(`/health ${res.status}`)
  return res.json()
}

export async function listWorkers(): Promise<Worker[]> {
  const res = await fetchJSON<WorkersResponse>('/api/workers')
  return res.workers
}

/* Pending permission as returned by the hub's /api/permissions endpoint. */
export interface PendingPermission {
  worker: string
  request_id: string
  tool_name: string
  description: string
  input_preview: string
}

export interface PendingResponse {
  pending: PendingPermission[]
}

export async function listPendingPermissions(): Promise<PendingPermission[]> {
  const res = await fetchJSON<PendingResponse>('/api/permissions')
  return res.pending
}

export async function resolvePermission(
  worker: string,
  request_id: string,
  behavior: 'allow' | 'deny',
): Promise<{ ok: boolean; request_id: string; error?: string }> {
  return fetchJSON('/api/permissions/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ worker, request_id, behavior }),
  })
}

/* Saved flow from the hub. */
export interface Flow {
  name: string
  body: string
  default_worker: string | null
  meta: Record<string, any>
  created_at: number
  updated_at: number
  last_run_at: number | null
}

export async function listFlows(): Promise<Flow[]> {
  const res = await fetchJSON<{ flows: Flow[] }>('/api/flows')
  return res.flows
}

export async function saveFlow(
  name: string,
  body: string,
  default_worker?: string | null,
): Promise<{ ok: boolean; flow?: Flow; error?: string }> {
  return fetchJSON('/api/flows', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, body, default_worker: default_worker ?? null }),
  })
}

export async function deleteFlow(name: string): Promise<{ ok: boolean }> {
  return fetchJSON(`/api/flows/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export async function runFlow(
  name: string,
  worker?: string,
): Promise<{ ok: boolean; run_id?: string; first_event_id?: string; error?: string }> {
  return fetchJSON(`/api/flows/${encodeURIComponent(name)}/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(worker ? { worker } : {}),
  })
}

export interface RunLog {
  run_id: string
  flow_name: string
  worker: string
  started_at: number
  finished_at: number | null
  first_event_id: string | null
  events: Array<{
    event_id: string
    ts: number
    kind: string
    content: string
    meta: Record<string, any>
  }>
}

export async function getRunLog(
  run_id: string,
): Promise<{ ok: boolean; run?: RunLog; error?: string }> {
  return fetchJSON(`/api/flows/runs/${encodeURIComponent(run_id)}`)
}

/* Pty bridge — WebSocket for the Integrated Terminal. Single-use ticket
 * exchange over HTTPS + Origin-allowlisted WS upgrade. */

export async function issuePtyTicket(worker: string): Promise<string> {
  const res = await fetchJSON<{ ok: boolean; ticket?: string; error?: string }>(
    '/api/pty-ticket',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ worker }),
    },
  )
  if (!res.ok || !res.ticket) throw new Error(res.error ?? 'ticket issuance failed')
  return res.ticket
}

export function ptyBridgeUrl(worker: string, ticket: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  return `${scheme}//${host}/ws/pty/${encodeURIComponent(worker)}?ticket=${encodeURIComponent(ticket)}`
}

/* Hub-wide settings (currently the recording defaults). */
export interface HubSettings {
  recording_enabled_by_default: boolean
  recording_max_bytes_per_file: number
  recording_keep_files: number
}

export async function getHubSettings(): Promise<HubSettings> {
  return fetchJSON<HubSettings>('/api/settings')
}

export async function updateHubSettings(
  patch: Partial<HubSettings>,
): Promise<HubSettings> {
  return fetchJSON<HubSettings>('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

/* Per-worker recording status. */
export interface RecordingFile {
  stem: string
  cast_path: string
  frames_path: string | null
  size_bytes: number
  modified: number
  active: boolean
}

export interface RecordingStatus {
  worker: string
  enabled: boolean
  active_file: string | null
  files: RecordingFile[]
  max_bytes_per_file?: number
  keep_files?: number
}

export async function getRecordingStatus(worker: string): Promise<RecordingStatus> {
  return fetchJSON<RecordingStatus>(
    `/api/workers/${encodeURIComponent(worker)}/recording`,
  )
}

export async function setRecordingEnabled(
  worker: string,
  enabled: boolean,
): Promise<RecordingStatus> {
  return fetchJSON<RecordingStatus>(
    `/api/workers/${encodeURIComponent(worker)}/recording`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    },
  )
}

/** Roll a worker's manager process — fixes "Worker silent" on managers
 * that started before live-terminal support shipped. */
export async function updateManager(
  worker: string,
  force = false,
): Promise<{ ok: boolean; error?: string; event_id?: string; state?: string }> {
  const qs = force ? '?force=true' : ''
  return fetchJSON(`/api/workers/${encodeURIComponent(worker)}/update-manager${qs}`, {
    method: 'POST',
  })
}

/** Open the hub-wide SSE stream. Returns a disposer; caller closes it to
 * detach. Unlike native EventSource, we can't set Authorization headers,
 * so v1 puts the bearer in a `?token=` query string. This is acceptable
 * for same-origin localhost; the ticket-exchange pattern replaces it
 * before any production deploy. */
export function subscribeEvents(
  onEvent: (event: string, data: any) => void,
  onError: (err: Error) => void,
): () => void {
  const token = getBearer()
  if (!token) {
    onError(new AuthError('no bearer set'))
    return () => {}
  }
  // TODO(ticket-exchange): replace with POST /api/pty-ticket issuance
  // before this UI is exposed beyond localhost.
  const url = `/api/events/stream?token=${encodeURIComponent(token)}`
  const source = new EventSource(url)

  // Default "message" fallback + known event kinds.
  const kinds = [
    'channel_event',
    'outbound',
    'permission_request',
    'permission_response',
    'interrupt',
    'closed',
  ]
  for (const kind of kinds) {
    source.addEventListener(kind, (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data)
        onEvent(kind, data)
      } catch {
        onError(new Error(`bad SSE payload on ${kind}: ${e.data}`))
      }
    })
  }

  source.onerror = () => {
    onError(new Error('SSE connection error'))
  }

  return () => source.close()
}
