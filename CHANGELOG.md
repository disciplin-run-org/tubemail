# Changelog

User-facing changes to TubeMail. Newest first.

## Unreleased

### Added

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
