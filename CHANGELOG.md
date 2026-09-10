# Changelog

User-facing changes to TubeMail. Newest first.

## Unreleased

### Added

- **A restarted worker no longer redoes work its previous session already
  finished.** When a session ends with a clean break — "save what you
  learned, then start a new task" — the replacement session starts with no
  memory of what came before. Catching up on messages that arrived during
  the restart, it could not tell a completed work order from a waiting one,
  so it re-ran the lot.

  A session that ends now leaves a mark on its own timeline first:
  everything above this line is settled. The replacement reads only what
  arrived after that mark, so it picks up genuinely pending work and leaves
  finished work alone. Nothing to configure — the session-boundary commands
  post the mark themselves.

  Two supporting changes come with it. Timelines show a session-boundary
  divider, and a worker whose last message was a work order now shows as
  idle once its session ends rather than staying stuck on "busy". And a
  restart that keeps its conversation still catches up the old way — the
  new rule applies only where there is no memory to check against.

- **Optional: let a worker run without permission prompts.** Unattended
  workers used to stall waiting for someone to approve a tool call. Set
  `TM_DANGEROUSLY_SKIP_PERMISSIONS=1` in any env-file layer (put it in
  `~/.config/tubemail/.env` to cover every worker on the machine) and
  `claude-tm` launches Claude Code with `--dangerously-skip-permissions`,
  so it runs start to finish without stopping to ask.

  **Off unless you turn it on.** Existing setups and fresh installs are
  unchanged — prompts still appear. The switch accepts `1`, `true`,
  `yes`, or `on`; anything else, including a typo, leaves prompts on.

  It bypasses *every* permission check, so only use it where something
  else already blocks dangerous commands — a deny hook, a container, or
  a disposable VM. The startup log says so whenever it is active.

### Fixed

- **`claude-tm` no longer fails to start in projects that have their own
  `.env`.** Launching `claude-tm` from a repo carrying an unrelated `.env`
  (a Vite frontend, for instance) died with `TUBEMAIL_SECRET is not set`
  even though the secret was sitting in `~/.config/tubemail/.env`. Env
  files are now layered: every candidate is read, nearest first, and the
  first file to define a key wins. A local `.env` supplies its own keys
  without hiding the global fallback. Shell variables still beat all files.

  If you worked around this by pasting `TUBEMAIL_SECRET` into a project
  `.env`, you can now delete that line and keep the secret in one place.
