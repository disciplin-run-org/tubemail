# TubeMail public-release checklist — 2026-04-27

Consolidated list. Companion to the longer reasoning doc at
`jjstack/launch-plan-20260425-145824.md` (positioning, competitive
landscape, hero options, risks).

## State

- Repo: `JesperJurcenoks/tubemail` — **private**, 0 stars, no
  description, no topics, no homepage URL.
- Code: v0.2.0 + 12 modified / 7 untracked files of audit work
  (SPA cache headers, dev/prod compose split, `task=True` on
  long-running tools, BUSY_QUIET_S + observed_active busy/idle
  algorithm, frame-delivery backoff, wait_for_matching_event
  request/response correctness fix, restart-manager UI button).
- Tests: 146 hub + 79 channel = 225 green.
- PyPI namespace: `tubemail` and `tubemail-hub` both still
  unregistered (404).
- Docker image: not on a public registry.
- Visual assets: none.
- Governance files: none (`SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `CHANGELOG.md` all missing).

Total wall-time to launch-ready: ≈4 hours of focused work, almost
all in Gates 0–2.

---

## Gate 0 — house-clean (new since 04-25, ≈30 min)

- [ ] Land the in-flight branch. Audit + bug-fix work has been
      sitting uncommitted across 12 + 7 files. The patch class is
      feat (new tool surface, behavior changes), so the bump is
      0.2.0 → 0.3.0.
- [ ] Decide whether `jjstack/` ships in the public repo or moves
      to a private branch. Spot-check for embarrassing reasoning,
      customer names, or unredacted incident details.

## Gate 1 — must-do BEFORE flipping public (≈90 min)

- [ ] `SECURITY.md` — disclosure path. Reference
      `jjstack/security-review.md` if it survives the spot-check;
      otherwise inline.
- [ ] `CONTRIBUTING.md` — dev install, test command, branch
      convention, PR style. ≤80 lines.
- [ ] `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1 verbatim.
- [ ] `CHANGELOG.md` — `0.2.0 (or 0.3.0)` initial public release
      bullets. Use as the GitHub Release body.
- [ ] Secret audit: `git log -p -G '(TUBEMAIL_SECRET|sk-|api[_-]?key)'`
      and a grep for internal hostnames in committed files.
- [ ] `gh repo edit` to set description, topics, homepage URL.
      Topics: claude-code, mcp, mcp-server, ai-agents,
      agent-orchestration, multi-agent, claude, anthropic, fastmcp,
      pty, developer-tools.
- [ ] Flip repo to public — last step, only after the rest is in.

## Gate 2 — README + visuals (≈60 min)

- [ ] Pick the hero. The 04-25 plan lays out three options;
      option A (*"Make any Claude Code session drive any other"*)
      is the safest. Decide and rewrite the opener.
- [ ] `docs/images/hero.png` — Workers tab with ≥2 workers
      visible. Single biggest README gap; every competitor leads
      with a screenshot.
- [ ] `docs/images/permissions.gif` — permission-inbox flow,
      ≤8 s, ≤2 MB. The feature competitors don't have. Lead with
      it.
- [ ] Comparison table — 4 rows max: TubeMail vs Claude Squad vs
      Conductor vs Anthropic Agent Teams. Include Agent Teams;
      dodging it reads defensive.
- [ ] Demote the install matrix below the screenshots.
- [ ] Move the tool-surface table to `docs/MCP_TOOLS.md`.

## Gate 3 — distribution (≈45 min)

- [ ] PyPI publish: `tubemail` (channel plugin package).
      `python -m build && twine upload`.
- [ ] PyPI publish: `tubemail-hub` (hub package).
- [ ] Tag `v0.2.0` (or `v0.3.0` after Gate 0 lands) and create the
      GitHub Release with the CHANGELOG entry as the body.
- [ ] Publish Docker image to GHCR:
      `ghcr.io/jesperjurcenoks/tubemail-hub:<ver>` and `:latest`.
      Optional but expected for an OSS hub. Add
      `.github/workflows/release.yml` for tag-driven publishing.

## Gate 4 — launch surface (≈45 min)

- [ ] Show HN draft (≤200 words). Empty-seat lede:
      *"Other tools manage parallel agents for you. TubeMail manages
      them for another agent."* Save to
      `jjstack/launch-show-hn-20260427.md`.
- [ ] X/Twitter thread draft (5 posts max).
- [ ] LinkedIn post draft (≤1500 chars).
- [ ] r/ClaudeAI post — community-focused, not promotional.
- [ ] One of r/LocalLLaMA / r/LangChain / r/AI_Agents — pick one.
- [ ] Open 5 "good first issue" tickets (set in 04-25 plan).

## Gate 5 — first 24 h after announce

- [ ] Watch issues every 2 h for the first 12 h.
- [ ] Pin a "welcome / what to try first" issue.
- [ ] Note where stars came from for future channel mix.
- [ ] Schedule a v0.x.1 patch within 72 h on first-day feedback.
      Signal "this project is alive."

---

## Open decisions (not blockers, call before announce)

1. Hero option A / B / C — recommend A.
2. Comparison table includes Anthropic Agent Teams — recommend yes.
3. Same-day vs Tuesday 9am US Eastern — ET is the classic HN slot.
4. PyPI namespace: two packages (`tubemail` + `tubemail-hub`) vs
   one with `tubemail[hub]` extra. Two matches what the README
   claims today.
5. Bump to 0.3.0 before public flip, or ship 0.2.0 and let the
   audit work be 0.3.0 next week.
