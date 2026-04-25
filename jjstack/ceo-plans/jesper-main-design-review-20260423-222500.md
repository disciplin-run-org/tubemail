# Design Review — tubemail web UI

Reviewing:
- Design doc: `jjstack/jesper-main-design-20260423-215025.md`
- CEO plan: `jjstack/ceo-plans/jesper-main-ceo-20260423-220000.md`
- Eng review: `jjstack/ceo-plans/jesper-main-eng-20260423-222000.md`

Scope: visual/UX review of the v1 caps (Roster, Permission Inbox,
Integrated Terminal) plus resolution of the clipboard-on-self-signed
UX gotcha flagged by engineering.

Status: **Ship, with the 10 decisions below locked.** No visual red
flags; the ecosystem conventions are strong enough to carry most of
the design, and the tubemail-specific surfaces (terminal pane, state
badges, pop-out) slot in cleanly.

---

## D1. Design tokens — inherit leanspecs, extend for tubemail

The ecosystem already has a design token set at
`leanspecs/frontend/src/styles.css` and the shared `variables.css`
referenced from `@ai-agents/shared-ui`. Reuse verbatim:

| Token | Value | Use in tubemail |
|---|---|---|
| `--primary` | `#1d4ed8` (blue) | Active nav, links, "save" and commit actions |
| `--ai` | `#7c3aed` (purple) | Reserved for AI actions; also `waiting_permission` state badge |
| `--success` | `#16a34a` (green) | `idle` state badge, ack-received |
| `--warning` | `#d97706` (amber) | `busy` state badge, time-since-inbound counter |
| `--danger` | `#991b1b` (red) | `offline` (unclean), errors, disconnect banners |
| `--gray-*` | 50/100/200/300/400/500/700/900 | Text, borders, disabled |

Extensions unique to tubemail:

| New token | Value | Use |
|---|---|---|
| `--offline-clean` | `--gray-400` | `offline` (exited cleanly) — desaturated, not alarming |
| `--terminal-bg` | `#0d1117` | Terminal pane background (GitHub-dark calibration) |
| `--terminal-fg` | `#c9d1d9` | Default terminal foreground |
| `--pty-attached` | `--primary` at 15% alpha | Badge bg for "N clients attached" indicator |

**Five distinct hues in play** (blue, purple, green, amber, red) + grays
+ the two terminal-specific colors. Within the "max 5 colors" rule
counting hues, not shades.

---

## D2. State badge iconography — one library, one style

Pick **Lucide** (MIT license, bundled with shared-ui convention in
ecosystem projects, ~1000 icons, all outlined, consistent stroke).
Ban mixing styles — no Material outlined + Lucide + hand-drawn.

State → badge composition (authoritative — ASCII wireframes later
in this doc use these same icons, any divergence is a bug in the
wireframe):

| State | Color token | Icon (Lucide name) | Text |
|---|---|---|---|
| `idle` | `--success` | `circle-check` | "idle" |
| `busy` | `--warning` | `loader-2` (spin) | "busy · 2m14s" |
| `waiting_permission` | `--ai` | `shield-alert` | "waiting (N)" |
| `offline` (unclean) | `--danger` | `circle-x` | "offline" |
| `offline` (clean) | `--offline-clean` | `circle-dashed` | "exited" |
| `unknown` | `--gray-400` | `circle-help` | "unknown" |

Badge chip inherits from shared-ui's `.shared-badge` class
(`leanspecs/frontend/src/styles.css` references `--radius: 4px` and
a standard 6px/2px padding). Tubemail does NOT invent a new chip —
it uses the shared one. Icon size: 12px; gap from icon to text: 4px.
One component, `<StateBadge state={...}/>`, used in every place a
state is shown (roster, terminal chrome, anywhere else).

---

## D3. Roster layout — one-line-per-worker discipline

Worker roster is the landing surface. Keep dense; every row is one line
unless expanded.

Wireframe (dots are the SSE-connected indicator — a filled
`--primary` 6px circle rendered separately from the state badge;
the state badge itself uses the D2 Lucide icons, shown as their
names here so ASCII doesn't drift from the component spec):

```
┌──────────────────────────────────────────────────────────────────────┐
│ tubemail  v0.1.0  ·  hub: healthy  ·  6 workers online               │  <- header strip
├──────────────────────────────────────────────────────────────────────┤
│ ▼ leanspecs (3)                                                      │
│   ● leanspecs-code-tm  [circle-check idle]              v0.1.0      │
│   ● leanspecs-spec-tm  [loader-2 busy · 2m14s] (1)      v0.1.0      │
│   ● leanspecs-ui-tm    [circle-check idle]              v0.1.0      │
│ ▼ iris-qa (1)                                                        │
│   ● iris-qa-tm         [shield-alert waiting (2)] (2)   v0.1.0      │
│ ▼ tubemail (1)                                                       │
│   ● tubemail-tm        [circle-check idle]              v0.1.0      │
└──────────────────────────────────────────────────────────────────────┘
```

**Grouping** by project name (left side of the `-<role>-tm` suffix);
same convention `tm_list_workers` already uses. Collapsible headers.

**Columns, left to right:** dot indicator (SSE connected), worker
name, state badge with time counter where applicable, pending-
permissions count, forwarder version.

**Time-since-inbound counter** lives inside the `busy` state badge,
formatted `busy · <N>m<N>s` up to 59m, then `busy · <N>h<N>m`.
Auto-updates every second via the global SSE stream (no per-row
setInterval — one ticker, all rows read shared clock).

**Click a row → opens the detail pane** (terminal + permission subpane)
as the main content. Sidebar nav: "Workers" (roster), "Permissions"
(inbox), "Saved Messages" (E2), "Settings".

**Mobile (responsive requirement):** collapse to card-per-worker stack,
truncate version column. Terminal access on mobile is degraded (see
D7); the roster still works as a dashboard.

---

## D4. Permission Inbox — keyboard-first, approve in ≤2 keys

Keyboard is the primary interaction; mouse is secondary.

```
┌─────────────────────────────────────────────────────────────────────┐
│ Pending permissions · 3 across 2 workers                            │
├─────────────────────────────────────────────────────────────────────┤
│ ▶ iris-qa-tm            Bash                             [ 45s ago ]│
│   pytest -xvs tests/test_status.py                                  │
│   [Y] allow    [N] deny    [shift+Y] allow all Bash on iris-qa-tm  │
├─────────────────────────────────────────────────────────────────────┤
│   leanspecs-spec-tm     Edit                            [ 12s ago ]│
│   specs/product.json                                                │
│   [Y] allow    [N] deny                                             │
├─────────────────────────────────────────────────────────────────────┤
│   iris-qa-tm            Bash                             [ 5s ago ] │
│   docker compose logs iris-qa --tail 50                             │
│   [Y] allow    [N] deny                                             │
└─────────────────────────────────────────────────────────────────────┘
```

**Focus model:**
- Page entry does NOT auto-focus a row. The inbox header is
  focused instead — user must explicitly press `Tab` or `↓` to
  start reviewing. Prevents the "reflex-Y on page load"
  footgun where a user who just alt-tabbed into the window
  rubber-stamps whatever is on top.
- `↑` / `↓` navigate rows.
- `Y` allows the focused row; `N` denies. **A 3-second undo toast
  slides in at the bottom of the viewport** (`[Esc] to undo`) —
  the allow/deny is queued, not sent, until the 3 s elapses.
  Escape during the window cancels it and restores the row.
  **Burst semantics:** pressing Y again on a *different* row
  before the prior toast times out does NOT flush the prior
  action — the prior action stays in its own 3 s window, and a
  second toast stacks above the first (up to 3 visible; beyond
  that, oldest auto-flushes). Each toast owns its own `Esc`
  context; the toast with keyboard focus takes the Esc. A fast
  Y/↓/Y/↓/Y sequence therefore queues three actions, all
  individually undoable. Implementation: a per-toast timer +
  an ordered list of pending-actions tied to event_ids, flushed
  independently as each timer fires.
- Focus after allow/deny moves to the *next* row by event_id, not
  by index. If a new row arrives under the cursor during the 3
  seconds, focus stays where it is; the incoming row appears above
  the focused one with a 1-second highlight flash so the operator
  notices.
- **Bulk actions require a two-step confirmation, not a sticky
  modifier.** "Bulk-allow all Bash on iris-qa-tm" appears as a
  distinct button (`[B] allow all 3 Bash prompts on iris-qa-tm`)
  that the user must explicitly activate; activation opens a
  confirm dialog (`[Enter] confirm, [Esc] cancel`). No
  Shift-modifier shortcut. Reason: one sticky shift key + one
  reflex `Y` = rubber-stamped fleet approval. Worth the extra
  keystroke to avoid that failure mode.

**Visual hints** for the shortcuts live inline (`[Y]` `[N]` chips),
styled as `--gray-200` background with `--gray-700` letterforms —
the same look as leanspecs's command chips so operators already know
this language.

**Age column** uses relative time (`5s ago`, `2m ago`), updated by
the same clock-ticker as the roster counter.

**Overdue signaling is multi-channel, not color-only.** Ages > 30 s:
- Tint the border-left `--danger` (red).
- Add a `clock` Lucide icon prefix to the age text with label
  `"overdue"` for screen readers (`aria-label="overdue — 45s ago"`).
- Text weight on the age column shifts to 600.
Three signals (color, icon, weight) cover colorblind operators.

**Empty state:** big check icon, `"All caught up"` in `--gray-500`.
Restful; not celebratory.

**Desktop notifications (E3)** fire when a `permission_request` event
lands and the tab is not focused. Notification body: `<worker> wants
to run <tool>`. Click → focus the tab with that row highlighted. 5-
second auto-dismiss.

---

## D5. Terminal pane chrome

Wireframe (ASCII glyphs below are illustrative only; real icons are
the Lucide names in the chrome-strip component list that follows —
not the ASCII characters in the wireframe):

```
┌───────────────────────────────────────────────────────────────────┐
│  leanspecs-spec-tm  [loader-2 busy · 2m14s]  · 2 attached        │  <- chrome strip
│                            [external-link] [rotate-cw] [x]        │
├───────────────────────────────────────────────────────────────────┤
│  $ claude-tm --role spec                                          │
│  > leanspecs-spec-tm session                                      │
│  …                                                                │
│  (xterm.js surface)                                               │
│                                                                   │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

Authoritative icon set for the chrome strip (Lucide names — the
wireframe above must be read through this lens):

**Chrome strip** (sticky top, 32 px):
- Worker name (clickable → roster).
- Live state badge (same component as roster — the state is the state).
- "N attached" indicator only when N > 1 (quiet when alone).
- Pop-out button (Lucide `external-link`).
- Reconnect button (Lucide `rotate-cw`) — only visible when WS is
  disconnected; otherwise hidden to avoid accidental clicks.
- Close button (Lucide `x`) — closes just this pane, not the session.

**Background:** `--terminal-bg` (#0d1117). xterm.js default palette
overridden to the GitHub-dark calibration so colors match the
ecosystem's dark surfaces.

**Disconnected banner:** slides in from the top of the terminal area
(not a modal), full-width, `--danger` left border, message "Pty bridge
disconnected — [Reconnect]". One button. Doesn't block interaction
with the chrome strip.

**Zoom indicator:** bottom-right corner, 10 px gray text, shows
current font size (`14px`) for 1 second after any Ctrl+=/Ctrl+-.
Auto-hides. Persistence per-worker-name in `localStorage` under
`tubemail.terminal.zoom.<worker>`.

---

## D6. Pop-out window — lean chrome, full terminal

Pop-out opens a new browser window with the terminal as the entire
viewport. No sidebar, no navigation — just the chrome strip and the
pty.

```
┌───────────────────────────────────────────────────────────────────┐
│  leanspecs-spec-tm  ·  busy · 2m14s           [⤴ back to hub]     │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│                                                                   │
│                    (xterm.js full viewport)                      │
│                                                                   │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

**Window title:** `<worker> · tubemail` — lets OS window-switcher
show the worker name directly (what the user is really asking for
when they say "four workers across a 4K display").

**Back-to-hub button:** closes the pop-out and refocuses the main
tab. Lucide `arrow-up-left`. Sits in the chrome strip.

**Keyboard profile:** identical to the embedded terminal. Zoom
persists per-worker-name across both the embedded and pop-out
views (they share `localStorage`). Saved Messages (E2) dropdown
is accessible via `Ctrl+Shift+M` — same shortcut in both places
so muscle memory carries.

**No footer** in the pop-out. It's a tool window, not a page.

---

## D7. Responsive behavior — honest about terminal-on-mobile

Responsive is a jjstack requirement (non-negotiable). Candid
accounting of what works at each breakpoint:

- **Desktop (≥ 1280 px).** Primary target. All caps full fidelity.
- **Tablet (768–1279 px).** Sidebar collapses to a hamburger menu.
  Permission Inbox drops the worker-group headers but keeps rows.
  Terminal pane is usable but cramped — xterm.js at 12px instead
  of 14.
- **Mobile (< 768 px).** Roster + Permission Inbox are fully usable
  and probably the primary mobile use case (approve a permission
  from a phone while away from the desk). The Permission Inbox
  switches to a **touch-optimized layout** below 768 px:
  - Rows become cards with generous padding (16 px inside).
  - Allow / Deny become full-width buttons, minimum 48×48 px
    (exceeds WCAG 2.2 AA 24×24 minimum, meets AAA 44×44).
  - Tool name + description stack vertically; no keyboard chip
    hints.
  - Swipe-right on a card = allow (with undo toast); swipe-left =
    deny. Standard iOS/Android gesture vocabulary.
  - Bulk actions are a full-screen modal, not a shortcut key.
  **Terminal pane is read-only on mobile** — no pty bridge.
  Keyboard input on a phone into a full-screen terminal is bad UX
  regardless of engineering heroics. Show a banner: *"Terminal is
  read-only on small screens — open on desktop for full access."*
  This degrades KR4 on mobile but that scenario is not the design
  target.

**Decision:** roster + permission inbox fully responsive with
touch-scale components below 768 px; terminal read-only < 768 px.

## D6b. Saved Messages (E2) wireframe

Accepted in the CEO review but not wireframed in the earlier draft.
Minimal surface:

```
(inside terminal pane or standalone composer)
┌─────────────────────────────────────────────────┐
│ [Ctrl+Shift+M] Saved Messages                   │  <- dropdown trigger in chrome strip
└─────────────────────────────────────────────────┘
        ↓ (opens as popover under the button)
┌─────────────────────────────────────────────────┐
│ Filter: [________________]                      │
├─────────────────────────────────────────────────┤
│ standard-qa-loop                                │
│ iris-qa-work-order                              │
│ leanspecs-spec-validate                         │
│ ──────────────────────────────                  │
│ [+ save current input as…]                     │
└─────────────────────────────────────────────────┘
```

- Trigger: `Ctrl+Shift+M` from any terminal context (embedded or
  pop-out); or click a small `bookmark` Lucide icon in the chrome
  strip.
- Content: filterable list, plain text names, newest-first. Each
  item is an entry in `localStorage.tubemail.saved_messages` as
  `{name: string, body: string, created_at: ts}`.
- Select: types the body into the active terminal via the pty
  bridge (same path as operator typing).
- "Save current input as…" captures whatever is in the terminal's
  prompt input at the moment of activation; prompts for a name;
  rejects duplicates (offers to overwrite).
- Delete: hover a row to reveal a `trash-2` button; confirms with
  an inline Escape-to-cancel pill, no modal.
- **Server-side persistence in v1 (upgraded 2026-04-24).**
  Quartermaster ships 2-3 weeks after tubemail v1; the Saved
  Messages cap is now a full flow shell with hub-side storage,
  not a localStorage stub. See D6c below for the expanded editor
  view.

## D6c. Flow editor view (expanded from the Saved Messages dropdown)

The popover in D6b is the *quick* surface. Clicking **Manage…**
at the bottom of the popover opens the flow editor as a full
main-content pane (replaces the terminal view while open).

```
┌───────────────────────────────────────────────────────────────────────┐
│ Flows                                         [+ New Flow]            │
├─ Flow list ─────────┬─ Editor ────────────────────────────────────────┤
│                     │                                                 │
│ standard-qa-loop  ▶ │ Name: standard-qa-loop                          │
│   last run: 2m ago  │ Target worker: [iris-qa-tm ▼]                   │
│                     │                                                 │
│ iris-qa-work-order  │ Body:                                           │
│   last run: —       │ ┌─────────────────────────────────────────────┐ │
│                     │ │ Run iris_qa_run on mcp:3.5.8 ...            │ │
│ leanspecs-validate  │ │ Classify failures as target/setup/iris-qa   │ │
│   last run: 1h ago  │ │ ...                                         │ │
│                     │ └─────────────────────────────────────────────┘ │
│                     │                                                 │
│                     │ [Save]  [Run now]  [Delete]                     │
│                     │                                                 │
│                     │ ── Run log (last 5) ──────────────────────      │
│                     │ 2m ago · iris-qa-tm · done · 205s · [view]     │
│                     │ 3h ago · iris-qa-tm · done · 198s · [view]     │
│                     │ 1d ago · iris-qa-tm · error · 12s · [view]     │
└─────────────────────┴─────────────────────────────────────────────────┘
```

**Visual reuse (no new primitives):**
- Flow list rows reuse the **Worker Roster row** styling — same chip
  chrome, same `--gray-50` hover, same active state (blue left-border).
- Target worker dropdown reuses the **roster row's state-badge**
  component inline (so the operator sees whether the selected worker
  is `idle` or `busy` before clicking Run now).
- Run log rows reuse the **Permission Inbox row** pattern (dense
  single-line rows with a `[view]` chip).
- The "Run now" button reuses the **Message Composer's "Send" style**
  — primary blue, keyboard-activatable with `Ctrl+Enter`.
- Keyboard: `↑` / `↓` navigate the flow list; `Enter` loads into the
  editor; `Ctrl+S` saves; `Ctrl+Enter` runs now.

**Chain editor is v1.1, not v1.** v1 flow = one `{worker, message}`
per flow. The effort estimate (5 days) assumes single-step flows.
Chains layer on later as a list of steps sharing a parent run_id;
the v1 data shape already accommodates that (see eng review Prereq D
endpoint sketch).

**Empty state** (no flows saved yet): centered `bookmark` icon, text
"Save your first flow from a terminal — type a message, then press
Ctrl+Shift+M → Save as…". Link to docs.

---

## D8. Clipboard-on-self-signed-HTTPS — the gotcha, resolved

The eng review flagged this as the one open UX decision. Context:
modern browsers gate the async Clipboard API behind a *secure context*.
Self-signed HTTPS is accepted by the user after a cert warning, but
browsers still flag the page "Not Secure" and **Chrome/Firefox
restrict the async Clipboard API even then** — the gesture is not
enough to override origin-security.

Three options:
- **A. Ship self-signed + `Ctrl+Shift+C` as the primary copy shortcut.**
  Ctrl+C still does SIGINT (standard xterm). Copy works, but via the
  "old" modifier. Deviates from the CEO plan's signature "Ctrl+C does
  OS copy when there's a selection" delighter.
- **B. Require a real cert before Ctrl+C copy works.** Ship two modes:
  (i) self-signed fallback where Ctrl+Shift+C is copy, and (ii)
  full-cert mode where Ctrl+C (with selection) copies via the
  Clipboard API. Detect at runtime from `navigator.clipboard` +
  `window.isSecureContext`; adapt the keybinding profile dynamically.
- **C. Use the legacy `document.execCommand('copy')` path** — works
  in any context but is deprecated and increasingly quirky.

**Decision: modified B (uniform keybinding, progressive enhancement).**

Original B (same keybind, different behavior at runtime) is a
footgun — muscle memory breaks when the same Ctrl+C does different
things on different machines. Fixed version:

- **`Ctrl+Shift+C` is the documented, always-works copy shortcut.**
  Works in every browser and every cert mode. Matches standard
  xterm convention; the thing you type when muscle memory kicks in.
- **`Ctrl+C`-with-selection is an opportunistic enhancement.** When
  the Clipboard API is available (real cert or localhost in a
  browser that trusts it), Ctrl+C-with-selection also copies. When
  it's not, Ctrl+C-with-selection does SIGINT (standard xterm
  behavior). The operator never has to relearn — Ctrl+Shift+C is
  the contract, Ctrl+C is a nicety.
- **No runtime keybinding flip.** The profile is fixed; only the
  *secondary* behavior of Ctrl+C varies, and the primary path
  (Ctrl+Shift+C) is identical everywhere.

**UI surface:** a small pill in the status bar showing
`🔒 trusted` or `⚠ self-signed` with a Lucide `shield-check` or
`shield-alert` icon. Text next to it: `Ctrl+Shift+C to copy`
(always). Click pill → help doc linking to `mkcert` for operators
who want the Ctrl+C enhancement; no pressure — the pill is
informational, not a nag.

This eliminates the "it worked yesterday" failure mode and still
lets users who invest in a real cert get the "every other app" feel
of Ctrl+C-with-selection as a bonus.

---

## D9. Typography — two fonts, Google Fonts only

jjstack constraint allows up to 5; we use 2. Cutting display from
the earlier draft because tubemail is a tool, not a marketing site,
and the ecosystem currently uses `system-ui` — one extra font adds
drift, three is gratuitous.

1. **Body/UI: Inter** (Google Fonts, SIL Open Font License). Clean,
   highly legible at small sizes. Used for every non-terminal
   surface including headers.
2. **Monospace/terminal: JetBrains Mono** (Google Fonts, Apache 2.0).
   Designed for code; excellent ligatures (can be disabled for
   strict terminals); ships with full ASCII + Latin-Extended. Used
   inside xterm.js and for any inline `<code>` in the UI.

No serif, no commercial fonts. Stack declarations in the design
tokens:

```css
--font-ui: 'Inter', system-ui, sans-serif;
--font-mono: 'JetBrains Mono', 'Courier New', monospace;
```

**Ecosystem-drift note:** leanspecs and iris-qa currently use
`system-ui` in the body. Tubemail is the first to adopt a named
Google Font. Two paths:

- Lead and let others follow — ship Inter here; when the next UI
  refresh in the ecosystem comes around, propose Inter as the
  shared-ui default via a single change in
  `shared/frontend/src/styles/variables.css`.
- Delay and align — keep `system-ui` in tubemail until the shared-
  ui change lands, then upgrade in lockstep.

Recommended: **lead.** Cost is one font. Shared-ui upgrade is a
10-line PR to the shared stylesheet once other projects are ready.
Don't hold v1 on ecosystem coordination; don't be loud about the
lead either.

System fallback is kept for offline dev; Google Fonts preconnect
+ `display=swap` so a slow CDN never blocks render.

---

## D10. Logos, favicon, navbar/footer consistency (jjstack constraints)

**Logo set — three variants needed:**
- **Horizontal** (main): "TubeMail" wordmark + a tube/pipe icon
  glyph. 240×48 px at 1x. For headers.
- **Square** (social/app): just the glyph, 512×512 source. For
  favicon, apple-touch-icon, social-card fallbacks.
- **Simplified/monochrome** (favicon 32×32, 16×16, dark/light
  mono variants): pared-down glyph readable at 16 px.

**Light + dark variants required.** The glyph works on both by
adjusting fill color; wordmark needs explicit light/dark
versions for contrast. Store in `frontend/public/logo/` as
labeled SVGs.

**Favicon package — explicit file list** (all under
`frontend/public/`):

- `favicon.ico` (multi-resolution: 16, 32, 48 px)
- `favicon-16x16.png`
- `favicon-32x32.png`
- `favicon-96x96.png`
- `apple-touch-icon.png` (180×180)
- `icon-192.png` (Android home screen)
- `icon-512.png` (PWA splash / Android home screen)
- `icon-maskable-512.png` (Android adaptive icon, safe-zone
  padding)
- `site.webmanifest` (references icon-192 and icon-512)
- `og-image.png` (1200×630, social card)
- `twitter-card.png` (1200×600, Twitter summary_large_image)

`index.html` `<head>` declares: favicon, apple-touch-icon,
manifest link, og:image, twitter:image. No CDN; all assets
shipped in the hub container.

Source: 1024×1024 master SVG in `frontend/src/assets/logo/`;
generate the raster set via a build-time script (one of:
sharp-cli, `realfavicongenerator`, or a 30-line ImageMagick
wrapper — pick during build, the output spec is what matters).

**Navbar consistency:** the sidebar is the navbar. Same across all
routes; active-nav indicator (blue left-border at 3 px, `--primary`)
already matches leanspecs convention. Sticky, never scrolls. On
mobile, collapses to a hamburger toggle top-left.

**Footer consistency:** ultra-minimal for this app — tubemail is a
tool, not a marketing site. One-line footer at the bottom of the
main content: `TubeMail v0.1.0 · <a>docs</a> · <a>source</a>`.
Same on every route. Pop-out windows: no footer.

**Logo generation path (v1 realistic):**

Concept: two concentric rounded-rect pipes with a small envelope
icon threading through the inner pipe. "Tube" (transport) +
"mail" (message) = the glyph. Rendered in `--primary` (blue) on
light backgrounds, `--bg` (white/gray-900) fill with
`--primary` stroke on dark.

Deliverables, named:

- `logo-horizontal-light.svg` (240×48) — pipe glyph + "TubeMail"
  wordmark in Inter 600, `--gray-900` text.
- `logo-horizontal-dark.svg` — same, `--text-strong` on dark.
- `logo-square-light.svg` / `-dark.svg` (512×512) — glyph only,
  centered with 20% safe-zone padding.
- `logo-mono-light.svg` / `-dark.svg` (64×64) — simplified
  glyph, single stroke, favicon-ready.

v1 placeholder plan: if the glyph isn't ready by build day, ship
the wordmark in Inter 700 on both light and dark ("TubeMail"
text, no icon) as `logo-horizontal-*.svg`. The favicon falls back
to a 32×32 circle with "TM" letters in `--primary`. Upgrade in
v1.1 without touching any code — replace the SVGs in place.

---

## D11. Design system deliverables (for Technical Specifications cap)

Artifacts to ship alongside v1 so a future contributor can extend
without drifting:

1. `frontend/src/styles/tokens.css` — all design tokens as CSS
   variables, imported from shared-ui where possible.
2. `frontend/src/components/StateBadge.tsx` — single component
   taking `state` prop, renders correct color + icon + text.
   Consumed by roster and terminal chrome.
3. `frontend/src/components/Chip.tsx` — keyboard-shortcut chip
   component (reused in permission inbox and elsewhere).
4. `frontend/public/logo/` — three variants × light/dark = 6 SVGs.
5. `frontend/public/icons/` — favicon package.
6. `frontend/README.md` — design token cheat-sheet (which token
   to use for what); ~30 lines.

---

## D12. Accessibility — up front, not post-hoc

For a state-heavy UI like this, a11y is a first-class concern, not
a release-gate retrofit. Locked before implementation starts:

1. **Focus-visible rings** on every interactive element. Token:
   `:focus-visible { outline: 2px solid var(--primary); outline-
   offset: 2px; }` — leanspecs already uses this, inherit verbatim.
2. **Skip-to-content** link as the first focusable element on every
   page. `href="#main"`, visually hidden until focused.
3. **ARIA-live regions** for dynamic lists:
   - Permission Inbox: `role="log"` with `aria-live="polite"` so
     screen readers announce "new permission request from iris-qa-
     tm, Bash" when a row arrives.
   - Roster: `aria-live="polite"` on state-change announcements
     (not on time-counter updates — those would flood the SR).
   - Disconnect banner: `role="alert"` (`aria-live="assertive"`)
     because it's an interruption.
4. **Contrast audit up front, not after.** Every token pair used
   for text-on-background must pass WCAG 4.5:1 for body text and
   3:1 for large text. Two pairs that need checking and may need
   adjustment:
   - `--gray-500` on white — 4.61:1, passes body.
   - `--warning` (#d97706) on white — 3.46:1, fails body (passes
     large-text only). For the `busy` badge, text is 0.75rem —
     borderline. Fix: either darken the warning token for
     tubemail-specific usage, or render the state text in
     `--gray-700` inside a warning-tinted chip.
5. **Keyboard-only path** must reach every capability:
   - Tab order: sidebar → main content → roster/inbox rows.
   - Terminal pane is a `<div tabindex="0">` wrapping xterm.js;
     entering focuses xterm; `Esc` exits focus back to chrome
     strip (so keyboard users aren't trapped).
6. **Screen-reader-only text** for icon-only buttons. Pop-out button
   includes `<span class="sr-only">Pop out terminal</span>`.
7. **Reduced motion** — respect `prefers-reduced-motion`. The
   `loader-2` spin on `busy` stops animating; the busy text still
   ticks up. No decorative animations anywhere.

Acceptance gate: **axe-core run in CI on the finished frontend
passes with zero violations** for WCAG 2.2 Level AA rules. Warnings
are fine; violations block. Add the axe-core step to the
`frontend-build` CI job named in the Eng Review.

## D13. Dark mode — v1, not v1.5

Earlier draft deferred this; that was wrong. With tokens defined
correctly, dark mode is a CSS variables flip under
`prefers-color-scheme: dark` + a manual toggle. One-day job during
v1, not a v1.5 project.

Spec:

```css
:root {
  --bg: white;
  --bg-elevated: var(--gray-50);
  --border: var(--gray-200);
  --text: var(--gray-700);
  --text-strong: var(--gray-900);
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1117;
    --bg-elevated: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --text-strong: #f0f6fc;
  }
}

:root[data-theme="dark"] {
  /* same as above, forced */
}
```

All component styles reference the semantic tokens (`--bg`,
`--border`, etc.), never the raw `--gray-*` tokens directly.

**Toggle lives in the sidebar footer** with three states
(`light`, `dark`, `auto`). `auto` (default) honors
`prefers-color-scheme`. Explicit `light` or `dark` sets
`data-theme` on `<html>` and persists in
`localStorage.tubemail.theme`. Override survives system theme
changes (if user picks `dark` on a system that later flips to
light, tubemail stays dark — that's the override contract).
Switching back to `auto` clears the override. Toggle UI: a tiny
3-segment selector with Lucide `sun` / `moon` / `monitor` icons.

**Terminal stays GitHub-dark in both modes** (it's always dark —
a terminal is a terminal). `--terminal-bg #0d1117` and the dark-
mode `--bg #0d1117` are intentionally the same hex; reference
them via two token names because their *purposes* are distinct
(terminal surface vs. UI background), which matters if we ever
want to retune one without the other.

## D14. Specialty page templates

Not a marketing site, so the list is short but must be spec'd:

- **404 Not Found.** Same sidebar chrome, main content shows a
  Lucide `frown` icon, "Page not found", and a link back to the
  roster. Simple.
- **Loading / initial connect.** Shown while the first SSE
  handshake completes. Centered spinner (reduced-motion:
  stationary dots), status text: "Connecting to hub…". If it
  takes >5 s, show the hub URL and "Is the hub running? Check
  `docker compose ps tubemail-hub`".
- **Permission denied / unauthenticated.** If the bearer is
  missing or invalid, show a page with a form to paste the
  bearer token; on submit, save to `localStorage` and retry.
- **Hub error.** 500-class response from any `/api/*` endpoint;
  show inline error banner with the hub's response body (if JSON,
  prettify; if HTML, show "Hub returned an HTML error page — see
  browser console for details").
- **Empty roster.** "No workers connected. Launch a worker with
  `claude-tm` from a project directory." Link to docs.

Each is a named component under `frontend/src/components/pages/`
so they're discoverable. Same footer and navbar as the rest of
the app (jjstack consistency rule).

## Reviewer concerns (persisted)

None blocking. One v1.5 item:

1. **Full-screen terminal "workspace mode"** — keyboard shortcut
   to hide the sidebar and let the embedded terminal be the whole
   viewport, without popping out. Different muscle memory than
   pop-out; some operators may prefer it. Defer to dogfood
   feedback.

---

## Score

**Iteration 3 score: 9/10.** Iteration 1 scored 6 (wireframe
glyph/spec drift, reflex-Y footgun, color-only overdue, mobile
touch targets, typography drift, clipboard runtime-keybinding
flip, dark mode deferred, specialty pages missing). Iteration 2
scored 8 strict (5 remaining gaps: undo-toast burst semantics,
E2 wireframe, wireframe-icon authority ambiguity, dark-mode
toggle persistence, favicon file list). All five addressed here:

- Undo-toast burst semantics: queued-per-row, stacked toasts,
  each with its own timer and Esc context.
- E2 wireframe: Saved Messages popover + Ctrl+Shift+M + localStorage
  schema spelled out under D6b.
- Wireframe-icon authority: authoritative Lucide names now
  annotated into the wireframe prose; ASCII characters marked as
  illustrative only.
- Dark-mode toggle: three states (light/dark/auto), persistence
  rule, override-wins-over-system-change contract documented.
- Favicon: explicit file list with resolutions, manifest
  references, and source pipeline.

Ready to hand back to the user with a consolidated summary of the
4-review pipeline. Remaining work for implementation is execution,
not design.
