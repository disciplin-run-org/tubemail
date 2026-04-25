# Security Review — tubemail

**Date:** 2026-04-24
**Scope:** Uncommitted diff + full CWE assessment of hub + channel plugin
**Assessor:** Claude Code + jjstack /security-review
**Sources:** Anthropic security-review, Sentry security-review, OWASP Top 10:2025

## Summary

- **Critical:** 0
- **High:** 2 (path traversal, timing-attack bearer)
- **Medium:** 2 (silent exception in state load, silent queue drop)
- **Low:** 2 (live-reload in prod, auth endpoint not rate-limited)
- **Filtered (confidence < 0.8):** 4 (response-code distinguishability, subprocess scan, `tm_keystroke` passthrough, CORS)

## Findings

### FINDING-001: Path traversal via worker name in persistent state write

- **Severity:** High
- **Confidence:** 0.95
- **CWE:** CWE-22 (Improper Limitation of a Pathname to a Restricted Directory)
- **OWASP:** A03 — Injection (path injection variant)
- **Location:** `src/tubemail_hub/bridge/engine.py:58-59`, `src/tubemail_hub/bridge/http.py:57-89`
- **Description:** `BridgeEngine._worker_file(name)` composes a filesystem path as `self._workers_dir / f"{name}.json"` without validating that `name` is a safe identifier. `name` comes from the URL path parameter `/tubemail/{worker}/*` and flows unchecked into `_persist()`, `_load_all()`, and `_worker_file()`. Default path converter `str` allows `.` and `..`, just not `/`. An attacker holding the bearer token can register worker `..` → writes `<workers_dir>/../.json` → escapes the intended directory. Chained via `_load_all()` glob + `model_validate`, a planted state file becomes a trusted in-memory `WorkerState` on next restart.
- **Exploit scenario:**
  1. Attacker obtains TUBEMAIL_SECRET (stolen from a compromised worker env, a log, etc.).
  2. `POST /tubemail/..%2F..%2Ftmp%2Fpwned/register` with body `{"cwd": "/", "pid": 1}` — written to `/data/tubemail/workers/../../tmp/pwned.json` = `/tmp/pwned.json`.
  3. Worse: register `worker=../../../app/evil` → `/app/evil.json` (container runs as `tubemail` user who owns `/app`) — combined with the live-reload mount this is a path to code-adjacent writes.
  4. Even without path escape: `_load_all()` reads **every** `.json` file in the workers dir with no authenticity check; a crafted file planted through any other means (e.g., Docker volume share) becomes trusted state.
- **Evidence:**
  ```python
  # engine.py:58-59
  def _worker_file(self, name: str) -> Path:
      return self._workers_dir / f"{name}.json"

  # http.py:57 — no validation before passing to engine:
  @router.post("/{worker}/register", dependencies=[Depends(auth_dep)])
  async def register(worker: str, body: RegisterRequest) -> RegisterResponse:
      cursor = await engine.register_worker(worker, body.cwd, ...)
  ```
- **Remediation:** add a worker-name validator that requires `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` (no leading dot, no slashes, no pipes, bounded length). Validate in `http.py` before the engine call AND in `engine.register_worker` as defense-in-depth. Reject with 400 on bad names. Also harden `_load_all` to ignore files whose stem doesn't match the validator.
- **Status:** FIXED in this session.

### FINDING-002: Timing-attack-vulnerable bearer comparison

- **Severity:** High
- **Confidence:** 0.95
- **CWE:** CWE-208 (Observable Timing Discrepancy)
- **OWASP:** A07 — Identification and Authentication Failures
- **Location:** `src/tubemail_hub/bridge/http.py:43`
- **Description:** `authorization.removeprefix("Bearer ").strip() != secret` uses `!=`, which short-circuits on the first differing character. An attacker on the same machine or same low-latency network can infer prefix bytes of TUBEMAIL_SECRET by timing the 401 response. Low-rate attack, but the channel plugin plan is about to broaden this attack surface substantially (every browser tab doing POSTs).
- **Exploit scenario:** network-adjacent attacker measures 401 latency for many guessed prefixes; statistical analysis recovers the secret bit-by-bit. Bound by TLS jitter + server-side noise, but feasible on localhost or LAN.
- **Evidence:**
  ```python
  # http.py:43
  if authorization.removeprefix("Bearer ").strip() != secret:
      raise HTTPException(...)
  ```
- **Remediation:** `hmac.compare_digest(provided, secret)` — constant-time compare.
- **Status:** FIXED in this session.

### FINDING-003: Silent exception swallow in state load

- **Severity:** Medium
- **Confidence:** 0.9
- **CWE:** CWE-755 (Improper Handling of Exceptional Conditions)
- **OWASP:** A09 — Security Logging and Monitoring Failures
- **Location:** `src/tubemail_hub/bridge/engine.py:67-68`
- **Description:** `_load_all()` catches every exception when parsing a worker state file and silently `continue`s. Corrupt state, schema drift, or a crafted file is discarded with no log. Violates the CLAUDE.md "no silent errors" rule. Combined with FINDING-001, an attacker who plants a malformed file could hide the intrusion because the failure leaves no trace.
- **Remediation:** log at `warning` level with the filename and a short exception repr, then continue. Never swallow.
- **Status:** FIXED in this session.

### FINDING-004: Silent queue-full drop in event fan-out

- **Severity:** Medium
- **Confidence:** 1.0
- **CWE:** CWE-755
- **OWASP:** A09
- **Location:** `src/tubemail_hub/bridge/engine.py:321-327`
- **Description:** `_fan_out()` does `except asyncio.QueueFull: pass` — a slow SSE subscriber silently loses events. For the planned web UI that streams pty bytes through this path, this becomes "occasional missing keystrokes, no error visible anywhere." Already identified in the engineering review as Prereq A.
- **Remediation:** log at `warning` + push a `{"event": "closed", "reason": "queue_full"}` sentinel so the subscriber iterator terminates cleanly.
- **Status:** FIXED in this session.

## Deferred (informational; below confidence threshold for HIGH severity)

### INFO-005: Live-reload `--reload` always enabled in the container

- **Location:** `entrypoint.sh`
- **Description:** Uvicorn runs with `--reload --reload-dir /app/src/tubemail_hub` in both HTTPS and HTTP branches. The source is mounted read-only from the host (`./src:/app/src:ro`), and the container user `tubemail` cannot write to it from inside. Low risk — but if the `:ro` mount option is ever removed (human error, config drift), the reload watcher becomes a write-to-reload RCE primitive. Not a bug today.
- **Remediation (future):** split dev vs prod entrypoint — dev runs `--reload`, prod does not. Or drop `--reload` from the baked entrypoint and re-enable it only in a compose override (the pattern the mcp-server skill recommends for dev mode).
- **Status:** documented; not fixed (lives in the "ship-ready plan" scope, not pre-v1).

### INFO-006: No rate limit on bearer-authed endpoints

- **Location:** `src/tubemail_hub/bridge/http.py:31-51`
- **Description:** An attacker can brute-force TUBEMAIL_SECRET at full server speed. Mitigation: TUBEMAIL_SECRET is generated via `secrets.token_urlsafe(32)` in `scripts/heal.py` (256 bits of entropy — brute force is infeasible regardless). Confidence of this being exploitable is below 0.8 because the keyspace is too large; flagged because the upcoming web UI will add more client-side auth endpoints (ticket issuance, etc.) that DO need rate limits for reasons other than secret guessing.
- **Remediation (future):** add per-IP rate limits to auth endpoints in the upcoming `/api/*` router (covered as a requirement in the engineering review).
- **Status:** documented; covered by future plan.

## Filtered (false positives or below confidence threshold)

- **`tm_keystroke` arbitrary byte input to pty** — intentional feature; caller is bearer-authed and the whole tool exists to inject input into the worker's pty. Not a vulnerability.
- **subprocess calls in `channel/src/tubemail/__init__.py`** — use argv list form with hardcoded args (`["git", "-C", str(repo_dir), "rev-parse", ...]`), no `shell=True`. Safe.
- **No CORS middleware configured** — FastAPI default is no CORS, which is safe. The upcoming web UI will need explicit CORS + Origin allowlist per the engineering review (that's additive design, not a current bug).
- **401 "missing bearer" vs "invalid bearer" distinguishable** — Anthropic precedent rule: auth-failure distinguishability for a static bearer scheme is not a finding. The attacker learns "you need a bearer here" either way.

## STRIDE Threat Summary

| Category | Findings | Key risks |
|---|---|---|
| **S** — Spoofing | 0 | Bearer model is sound; no spoofing surface beyond auth itself. |
| **T** — Tampering | 1 | Path-traversal write (F-001) lets bearer-holder tamper with disk state. |
| **R** — Repudiation | 1 | Silent state-load exception (F-003) hides evidence of tampering/corruption. |
| **I** — Information Disclosure | 1 | Timing-attack bearer (F-002) leaks the secret bit-by-bit. |
| **D** — Denial of Service | 1 | Silent fan-out drop (F-004) allows quiet event loss; real DoS surface comes in with the WS pty bridge (covered by future plan). |
| **E** — Elevation of Privilege | 1 | F-001 chained with `_load_all` yields arbitrary WorkerState injection at hub restart. |

**Uncovered categories:** none fully uncovered. The S/Spoofing surface is the smallest and grows with the upcoming web UI (Origin whitelist + ticket-exchange already in the eng plan address it).

## Web UI plan cross-check (planned but not yet built)

The engineering and design reviews already specify these as prereqs / design decisions — the security review confirms they are load-bearing:

- **Origin whitelist on WS upgrade** — required. F-001 + a same-site attacker would be the first real spoofing threat; Origin check closes it before it appears.
- **TLS-only on `/ws/pty/*`** — required. Keystroke confidentiality.
- **Ticket-exchange for WS auth** — required. Prevents bearer from landing in URLs / logs.
- **Rate limit on `/api/pty-ticket`** — required. Prevents ticket scanning.
- **Per-IP rate limit on WS upgrade** — required (raised in eng review iteration 2). Complements ticket-exchange.

No new findings beyond those already in the eng plan.

---

## Claude config self-audit

Quick check of `.claude/settings.json`, `CLAUDE.md`, `.mcp.json` in the tubemail repo.

- No `.claude/settings.json` in the tubemail repo (per `ls` — not found).
- `CLAUDE.md` — project-level file exists at the ai-agents monorepo root. Contents audited earlier in this session; no "always approve" / "skip verification" instructions. Clean.
- `.mcp.json` — not present in tubemail repo.

No config-audit findings.
