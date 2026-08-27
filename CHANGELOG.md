# Changelog

User-facing changes to TubeMail. Newest first.

## Unreleased

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
