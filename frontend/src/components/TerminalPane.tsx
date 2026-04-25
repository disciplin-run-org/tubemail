import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ExternalLink, RefreshCw, RotateCw, X } from 'lucide-react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { issuePtyTicket, listWorkers, ptyBridgeUrl, updateManager, type Worker } from '../api'
import { StateBadge, uiState } from './StateBadge'

interface TerminalPaneProps {
  worker: Worker
  now: number
  onClose: () => void
}

const ZOOM_KEY = (w: string) => `tubemail.terminal.zoom.${w}`
const DEFAULT_FONT_SIZE = 14

/** xterm.js-backed pty bridge for one worker. Bidirectional: browser
 * keystrokes go out as binary WS frames; pty output arrives as binary
 * WS frames. Ctrl+Shift+C/V is the always-works copy/paste shortcut;
 * Ctrl+C with a selection copies via the Clipboard API when available
 * (secure-context only), otherwise falls through to xterm's default
 * SIGINT behavior. */
export function TerminalPane({ worker, now, onClose }: TerminalPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const termRef = useRef<Terminal | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const [disconnected, setDisconnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // True until the first byte arrives from the manager. Used to show
  // a friendly "Connecting…" overlay instead of a black void —
  // distinguishes the "still attaching" state from "attached but
  // worker silent" state.
  const [hasReceivedBytes, setHasReceivedBytes] = useState(false)
  // After 4s of silence post-connect, surface a hint that the manager
  // probably needs a restart to pick up the streaming code.
  const [staleHint, setStaleHint] = useState(false)
  // Clipboard API requires a secure context. Probed once at mount.
  const clipboardAvailable =
    typeof navigator !== 'undefined'
    && !!navigator.clipboard
    && typeof window !== 'undefined'
    && window.isSecureContext
  const [fontSize, setFontSize] = useState<number>(() => {
    const raw = localStorage.getItem(ZOOM_KEY(worker.name))
    return raw ? parseInt(raw, 10) || DEFAULT_FONT_SIZE : DEFAULT_FONT_SIZE
  })

  const connect = useCallback(async () => {
    setError(null)
    setDisconnected(false)
    setHasReceivedBytes(false)
    setStaleHint(false)
    try {
      // Tear down any prior connection so we don't leave a zombie WS
      // attached to the hub while we open a fresh one.
      wsRef.current?.close()
      const ticket = await issuePtyTicket(worker.name)
      const ws = new WebSocket(ptyBridgeUrl(worker.name, ticket))
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      ws.onopen = () => {
        // Don't proactively send our size on connect. The manager will
        // post the canonical pty size via a text control frame; we
        // conform to that. Sending FitAddon's size first would race
        // with the manager's echo and the user would see two resizes.
      }
      ws.onmessage = (e) => {
        if (typeof e.data === 'string') {
          // Control message from manager — JSON {kind, ...}.
          try {
            const msg = JSON.parse(e.data)
            if (msg.kind === 'size' && msg.cols && msg.rows) {
              // Resize xterm to match what claude is rendering for.
              // First arrival also doubles as "we're attached" — drop
              // the connecting overlay even before the first byte
              // arrives, so the user sees the size sync feedback.
              termRef.current?.resize(msg.cols, msg.rows)
              setHasReceivedBytes(true)
              setStaleHint(false)
              // Popout mode only: grow the popout window so the full
              // pty grid fits without scroll. The default open size of
              // 1000x700 truncates the bottom rows when the manager
              // pty is taller than ~38 rows. window.opener is set
              // because the parent app's popOut() used window.open().
              if (window.opener && termRef.current) {
                resizePopoutToFitPty(termRef.current, msg.cols, msg.rows)
              }
            }
          } catch {
            // Unknown control payload — ignore.
          }
          return
        }
        const bytes = new Uint8Array(e.data as ArrayBuffer)
        if (bytes.byteLength > 0) {
          setHasReceivedBytes(true)
          setStaleHint(false)
        }
        termRef.current?.write(bytes)
      }
      ws.onerror = () => setError('WebSocket error')
      ws.onclose = () => setDisconnected(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setDisconnected(true)
    }
  }, [worker.name])

  // Mount xterm + connect.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const term = new Terminal({
      fontFamily: "'JetBrains Mono', 'Courier New', monospace",
      fontSize,
      theme: {
        background: '#0d1117',
        foreground: '#c9d1d9',
        cursor: '#c9d1d9',
      },
      cursorBlink: true,
      convertEol: false,
      scrollback: 5000,
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(el)
    fit.fit()
    termRef.current = term
    fitRef.current = fit

    // Keystrokes → server. xterm.onData gives us the terminal-encoded bytes.
    term.onData((data: string) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(new TextEncoder().encode(data))
      }
    })

    // Keyboard selection state — xterm.js has no built-in keyboard
    // selection mode, so we track a (anchorRow, cursorRow) pair and
    // call term.selectLines on each Shift+Up/Down.  Single-row
    // shift-arrow selections use term.select.  Anchor resets on any
    // non-shift, non-ctrl key.
    type Anchor = { row: number; col: number }
    let selAnchor: Anchor | null = null
    let selCursor: Anchor | null = null

    const writeToPty = (text: string) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(new TextEncoder().encode(text))
      }
    }
    const copySelection = (clearAfter: boolean): boolean => {
      const sel = term.getSelection()
      if (!sel) return false
      if (clipboardAvailable) {
        navigator.clipboard.writeText(sel).catch(() => {/* silent */})
      }
      if (clearAfter) {
        term.clearSelection()
        selAnchor = null
        selCursor = null
      }
      return true
    }
    const writeClipboardToPty = () => {
      if (!clipboardAvailable) return
      navigator.clipboard
        .readText()
        .then((text) => writeToPty(text))
        .catch(() => {})
    }
    const cursorRow = () => term.buffer.active.cursorY + term.buffer.active.viewportY
    const cursorCol = () => term.buffer.active.cursorX

    // Keyboard interception — desktop-feel selection + clipboard.
    term.attachCustomKeyEventHandler(
      (ev: KeyboardEvent): boolean => {
        if (ev.type !== 'keydown') return true

        // ── Selection: Shift + arrow / Home / End ───────────────────
        // Shift held: extend selection. Don't pass to pty (no terminal
        // app should see Shift+arrow with our selection mode active).
        if (ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
          const arrow =
            ev.key === 'ArrowUp' ? 'up' :
            ev.key === 'ArrowDown' ? 'down' :
            ev.key === 'ArrowLeft' ? 'left' :
            ev.key === 'ArrowRight' ? 'right' :
            ev.key === 'Home' ? 'home' :
            ev.key === 'End' ? 'end' : null
          if (arrow) {
            if (selAnchor === null) {
              selAnchor = { row: cursorRow(), col: cursorCol() }
              selCursor = { ...selAnchor }
            }
            const cols = term.cols
            if (arrow === 'left') selCursor!.col = Math.max(0, selCursor!.col - 1)
            else if (arrow === 'right') selCursor!.col = Math.min(cols - 1, selCursor!.col + 1)
            else if (arrow === 'up') selCursor!.row = Math.max(0, selCursor!.row - 1)
            else if (arrow === 'down') selCursor!.row = selCursor!.row + 1
            else if (arrow === 'home') selCursor!.col = 0
            else if (arrow === 'end') selCursor!.col = cols - 1
            applySelection(term, selAnchor, selCursor!)
            return false
          }
        }

        // Any non-shift key (other than the ctrl combos below) ends
        // the selection mode — anchor resets on next Shift+arrow.
        const isModifierOnly =
          ev.key === 'Shift' || ev.key === 'Control' ||
          ev.key === 'Alt' || ev.key === 'Meta'
        if (!ev.shiftKey && !isModifierOnly) {
          selAnchor = null
          selCursor = null
        }

        // ── Clipboard ───────────────────────────────────────────────
        // Ctrl+A — select everything in the buffer.
        if (ev.ctrlKey && !ev.shiftKey && (ev.key === 'a' || ev.key === 'A')) {
          term.selectAll()
          return false
        }
        // Ctrl+Shift+C — always copies (standard xterm).
        if (ev.ctrlKey && ev.shiftKey && (ev.key === 'C' || ev.key === 'c')) {
          copySelection(false)
          return false
        }
        // Ctrl+Shift+V — always pastes (standard xterm).
        if (ev.ctrlKey && ev.shiftKey && (ev.key === 'V' || ev.key === 'v')) {
          writeClipboardToPty()
          return false
        }
        // Ctrl+C with selection → copy + clear; without → SIGINT.
        if (ev.ctrlKey && !ev.shiftKey && (ev.key === 'c' || ev.key === 'C')) {
          if (copySelection(true)) return false
          return true
        }
        // Ctrl+X with selection → copy + clear (terminal output is
        // read-only, so "cut" is functionally the same as copy
        // followed by clearing the visual selection). Without a
        // selection: pass through (apps may use Ctrl+X for their own
        // shortcuts; e.g. Emacs prefix).
        if (ev.ctrlKey && !ev.shiftKey && (ev.key === 'x' || ev.key === 'X')) {
          if (copySelection(true)) return false
          return true
        }
        // Ctrl+V → paste from clipboard.
        if (ev.ctrlKey && !ev.shiftKey && (ev.key === 'v' || ev.key === 'V')) {
          writeClipboardToPty()
          return false
        }
        // Ctrl+= / Ctrl+- / Ctrl+0 — zoom.
        if (ev.ctrlKey && !ev.shiftKey && (ev.key === '=' || ev.key === '+')) {
          setFontSize((s) => Math.min(32, s + 1))
          return false
        }
        if (ev.ctrlKey && !ev.shiftKey && ev.key === '-') {
          setFontSize((s) => Math.max(8, s - 1))
          return false
        }
        if (ev.ctrlKey && !ev.shiftKey && ev.key === '0') {
          setFontSize(DEFAULT_FONT_SIZE)
          return false
        }
        // Esc clears selection if one is active.
        if (ev.key === 'Escape' && term.getSelection()) {
          term.clearSelection()
          selAnchor = null
          selCursor = null
          return false
        }
        return true
      },
    )

    connect()

    // No browser-driven resize: the manager owns the pty size and
    // posts it via a text control frame. The browser xterm conforms
    // to that — overriding FitAddon's pane-fit guess. This keeps the
    // real terminal and the web UI rendering at the same dimensions
    // (claude can only render for one size at a time).

    return () => {
      wsRef.current?.close()
      term.dispose()
      termRef.current = null
      fitRef.current = null
    }
  // We intentionally omit connect/clipboardAvailable from deps — they don't
  // change after mount for a given worker, and re-mounting on every change
  // would tear down the terminal.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [worker.name])

  // Apply font-size changes without rebuilding the terminal.
  useEffect(() => {
    if (termRef.current) termRef.current.options.fontSize = fontSize
    try { fitRef.current?.fit() } catch {}
    localStorage.setItem(ZOOM_KEY(worker.name), String(fontSize))
  }, [fontSize, worker.name])

  // After 4s without bytes, hint that the manager probably needs a
  // restart. The pty-streaming code shipped 2026-04-24 — managers
  // started before that date silently ignore pty_attach events.
  useEffect(() => {
    if (hasReceivedBytes || disconnected) return
    const id = setTimeout(() => setStaleHint(true), 4000)
    return () => clearTimeout(id)
  }, [hasReceivedBytes, disconnected, worker.name])

  const [rolling, setRolling] = useState(false)
  const [rollPhase, setRollPhase] = useState<string>('')
  const [rollError, setRollError] = useState<string | null>(null)
  const rollManagerThenReconnect = useCallback(async (force = false) => {
    setRolling(true)
    setRollError(null)
    setRollPhase('Rolling manager…')
    try {
      // Tear down the current WS so the hub stops fanning bytes to a
      // dead consumer while the manager re-execs.
      wsRef.current?.close()

      const r = await updateManager(worker.name, force)
      if (!r.ok) {
        setRollError(r.error ?? 'manager update failed')
        return
      }

      // Wait for the OLD manager to exit (types /exit into claude,
      // claude exits, python exits with code 42). 1.5s is the minimum
      // we'd ever see on this machine; less and we'd race the new
      // manager registering as the same name and the hub's "evict
      // duplicate" logic would close the WRONG subscription.
      setRollPhase('Waiting for old manager to exit…')
      await new Promise((res) => setTimeout(res, 1500))

      // Now poll the worker list until the NEW manager comes back
      // online — bash wrapper re-execs python, which re-imports
      // tubemail, registers, subscribes to SSE. Up to 25s. Without
      // this poll the WS reconnect can fire BEFORE the new manager
      // has subscribed, so the hub's pty_attach event gets dropped
      // (no SSE subscriber to fan to) and the operator sees a black
      // screen even though everything else is healthy.
      setRollPhase('Waiting for new manager…')
      const deadline = Date.now() + 25_000
      const managerName = `${worker.name}-manager`
      let managerOnline = false
      while (Date.now() < deadline) {
        try {
          const ws = await listWorkers()
          const mgr = ws.find((w) => w.name === managerName)
          if (mgr?.online) {
            managerOnline = true
            break
          }
        } catch {
          // Hub blip — keep polling.
        }
        await new Promise((res) => setTimeout(res, 500))
      }
      if (!managerOnline) {
        setRollError(
          'manager did not come back online within 25s — it may still ' +
          'be starting; click Reconnect manually.',
        )
        return
      }

      // Settle: SSE subscription completes a tick after register.
      // 750ms is empirically enough — without it the pty_attach event
      // can race the subscribe and get fanned to zero subscribers.
      setRollPhase('Reconnecting…')
      await new Promise((res) => setTimeout(res, 750))
      connect()
    } catch (e) {
      setRollError(e instanceof Error ? e.message : String(e))
    } finally {
      setRolling(false)
      setRollPhase('')
    }
  }, [worker.name, connect])

  const state = uiState(worker)
  const busySince = state === 'busy' ? worker.last_activity : undefined
  const popOut = useCallback(() => {
    const url = `${window.location.origin}/?worker=${encodeURIComponent(worker.name)}&popout=1`
    // Initial size targets a typical full-screen claude pty (~200x50
    // at 14px). resizePopoutToFitPty will refine once the manager
    // posts its actual size, but starting close avoids the "bottom
    // row hidden until I drag" first-impression bug.
    const initW = Math.min(1280, Math.floor(window.screen.availWidth * 0.9))
    const initH = Math.min(900, Math.floor(window.screen.availHeight * 0.9))
    window.open(
      url,
      `tubemail-${worker.name}`,
      `width=${initW},height=${initH}`,
    )
  }, [worker.name])

  return (
    <div className="terminal-pane">
      <div className="terminal-chrome">
        <div className="terminal-chrome-left">
          <button
            type="button"
            className="back-btn"
            title="Back to worker list"
            onClick={onClose}
          >
            <ArrowLeft size={14} />
            <span>Workers</span>
          </button>
          <span className="title">{worker.name}</span>
          <StateBadge state={state} busySince={busySince} now={now} />
          {!clipboardAvailable && (
            <span className="pill warn" title="Clipboard API not available — use Ctrl+Shift+C">
              Ctrl+Shift+C to copy
            </span>
          )}
        </div>
        <div className="terminal-chrome-right">
          <button
            type="button"
            className="icon-btn"
            title="Roll manager (re-exec the manager process; Claude session preserved)"
            onClick={() => rollManagerThenReconnect(false)}
            disabled={rolling}
          >
            <RefreshCw size={14} />
          </button>
          <button type="button" className="icon-btn" title="Pop out (open in new window)" onClick={popOut}>
            <ExternalLink size={14} />
          </button>
          {disconnected && (
            <button type="button" className="icon-btn" title="Reconnect" onClick={connect}>
              <RotateCw size={14} />
            </button>
          )}
          <button type="button" className="icon-btn" title="Close pane" onClick={onClose}>
            <X size={14} />
          </button>
        </div>
      </div>
      {disconnected && (
        <div className="terminal-disconnected" role="alert">
          Pty bridge disconnected
          {error && <span> · {error}</span>}
          <button type="button" className="btn-secondary" onClick={connect}>
            <RotateCw size={12} /> Reconnect
          </button>
        </div>
      )}
      <div ref={containerRef} className="terminal-host" />
      {!hasReceivedBytes && !disconnected && (
        <div className="terminal-connecting" role="status">
          <div className="terminal-connecting-card">
            <p className="title">
              {staleHint ? 'Worker silent' : 'Connecting to ' + worker.name + '…'}
            </p>
            {staleHint ? (
              <>
                <p>
                  No data from the manager in 4s. The manager process for
                  this worker likely started before live-terminal support
                  shipped (2026-04-24) and is silently ignoring the
                  attach event.
                </p>
                <p>
                  Roll the manager to pick up the latest code — the
                  Claude session is preserved (the manager re-execs with
                  <code>--continue</code>).
                </p>
                {rollError && (
                  <div className="permission-error" role="alert" style={{ margin: '0.5rem 0' }}>
                    {rollError}
                    {rollError.includes('not idle') && (
                      <>
                        {' '}
                        <button
                          type="button"
                          className="btn-secondary"
                          style={{ marginLeft: '0.5rem' }}
                          onClick={() => rollManagerThenReconnect(true)}
                          disabled={rolling}
                        >
                          Force-roll anyway
                        </button>
                      </>
                    )}
                  </div>
                )}
                <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => rollManagerThenReconnect(false)}
                    disabled={rolling}
                  >
                    <RefreshCw size={12} />
                    {rolling
                      ? ` ${rollPhase || 'Rolling…'}`
                      : ' Roll manager + reconnect'}
                  </button>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={connect}
                    disabled={rolling}
                  >
                    <RotateCw size={12} /> Reconnect only
                  </button>
                </div>
              </>
            ) : (
              <p>
                Waiting for the worker's pty bridge. If nothing renders
                in a few seconds, you'll get an option to roll the
                manager.
              </p>
            )}
          </div>
        </div>
      )}
      <div className="terminal-zoom-indicator" aria-hidden="true">
        {fontSize}px
      </div>
    </div>
  )
}

/** Grow the popout window so the full pty grid fits. The manager's
 * pty is whatever size the operator's real terminal was when claude-tm
 * launched — typically wider/taller than a default 1000x700 popup,
 * so the user has to drag the window bigger before the prompt is even
 * visible. Compute target inner dimensions from the rendered cell
 * size and call resizeTo. Capped to the screen so we don't push the
 * window off-screen on small monitors.
 */
function resizePopoutToFitPty(term: Terminal, cols: number, rows: number): void {
  // xterm's "actualCellWidth/Height" properties live on the renderer
  // internals; the safer public route is dimensions via the wrapping
  // host element's computed cell size after a render. xterm exposes
  // _core.dimensions but that's private API. We approximate from the
  // font size — close enough for window sizing.
  const fontSize = term.options.fontSize ?? 14
  // Empirically, JetBrains Mono ratios:
  const cellW = Math.ceil(fontSize * 0.6)
  const cellH = Math.ceil(fontSize * 1.22)
  // Chrome strip + popout padding + scrollbar slack.
  const chromeH = 60
  const paddingW = 24
  const targetInnerW = cols * cellW + paddingW
  const targetInnerH = rows * cellH + chromeH
  // Convert inner dims to outer dims. Most browsers expose
  // outerWidth/Height vs innerWidth/Height — use the delta.
  const chromeOuterDeltaW = window.outerWidth - window.innerWidth
  const chromeOuterDeltaH = window.outerHeight - window.innerHeight
  const targetW = Math.min(
    targetInnerW + chromeOuterDeltaW,
    Math.floor(window.screen.availWidth * 0.95),
  )
  const targetH = Math.min(
    targetInnerH + chromeOuterDeltaH,
    Math.floor(window.screen.availHeight * 0.95),
  )
  // Only grow — never shrink. If the user dragged it bigger, leave it.
  const newW = Math.max(window.outerWidth, targetW)
  const newH = Math.max(window.outerHeight, targetH)
  if (newW !== window.outerWidth || newH !== window.outerHeight) {
    try {
      window.resizeTo(newW, newH)
    } catch {
      // Some browsers block resizeTo for windows the script didn't
      // open; window.opener-checked callers get past that, but if
      // not, just fail silently.
    }
  }
}

/** Render a keyboard-driven selection between anchor and cursor.
 * xterm.js exposes `term.select(col, row, length)` which addresses
 * cells starting at (col, row) and runs `length` cells forward,
 * wrapping across rows. We compute the total cells between two
 * points and emit one call. Anchor/cursor are normalized so the
 * earlier coord becomes the start regardless of selection direction.
 */
function applySelection(
  term: Terminal,
  anchor: { row: number; col: number },
  cursor: { row: number; col: number },
): void {
  const cols = term.cols
  const a = anchor.row * cols + anchor.col
  const b = cursor.row * cols + cursor.col
  const start = Math.min(a, b)
  const end = Math.max(a, b)
  const length = Math.max(1, end - start + 1)
  const startRow = Math.floor(start / cols)
  const startCol = start % cols
  try {
    term.select(startCol, startRow, length)
  } catch {
    term.clearSelection()
  }
}
