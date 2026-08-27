"""Unit tests for the ``claude-tm`` console_script wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from tubemail import claude_tm


def test_parse_args_extracts_role_separate_value():
    role, rest = claude_tm._parse_args(["--role", "spec", "--continue"])
    assert role == "spec"
    assert rest == ["--continue"]


def test_parse_args_extracts_role_equals_form():
    role, rest = claude_tm._parse_args(["--role=spec", "extra"])
    assert role == "spec"
    assert rest == ["extra"]


def test_parse_args_no_role():
    role, rest = claude_tm._parse_args(["--continue", "-p", "foo"])
    assert role == ""
    assert rest == ["--continue", "-p", "foo"]


def test_parse_args_role_without_value_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        claude_tm._parse_args(["--role"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--role requires a value" in err


def test_resolve_session_name_from_cwd(tmp_path, monkeypatch):
    project = tmp_path / "leanspecs"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv("TM_WORKER_NAME", raising=False)
    assert claude_tm._resolve_session_name("") == "leanspecs-tm"


def test_resolve_session_name_with_role(tmp_path, monkeypatch):
    project = tmp_path / "leanspecs"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv("TM_WORKER_NAME", raising=False)
    assert claude_tm._resolve_session_name("spec") == "leanspecs-spec-tm"


def _read_mcp_json(path: Path) -> dict:
    import json as _json
    return _json.loads(path.read_text())


def test_mcp_bootstrap_writes_to_user_home(tmp_path, monkeypatch):
    """Fresh install (Andre's case): no .mcp.json anywhere. The bootstrap
    writes the entry to `~/.mcp.json` (user-global) — NOT to the project
    cwd. Writing into the project file would dirty the git tree of every
    consumer repo across the ecosystem (QM #430)."""
    project = tmp_path / "proj"
    project.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv("TM_SKIP_MCP_BOOTSTRAP", raising=False)

    written = claude_tm._ensure_mcp_channel_entry(
        project_root=project, user_home=user_home
    )

    assert written == str(user_home / ".mcp.json")
    cfg = _read_mcp_json(user_home / ".mcp.json")
    assert cfg["mcpServers"]["tubemail-channel"] == {"command": "tubemail"}
    # Project file is NOT created — the whole point of QM #430's fix.
    assert not (project / ".mcp.json").exists()


def test_mcp_bootstrap_leaves_project_mcp_json_untouched(tmp_path, monkeypatch):
    """When the project already has its own canonical `.mcp.json` (e.g.
    leanspecs's `leanspecs` block), the bootstrap must NOT mutate it.
    The dirty-tree symptom that QM #430 surfaced was every consumer repo
    showing `M .mcp.json` forever — the fix is to write user-global
    only, never project-local."""
    import json as _json
    project = tmp_path / "proj"
    project.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.chdir(project)

    project_original = {
        "mcpServers": {
            "leanspecs": {"type": "http", "url": "http://localhost:8003/mcp/"},
        },
    }
    (project / ".mcp.json").write_text(_json.dumps(project_original, indent=2))

    written = claude_tm._ensure_mcp_channel_entry(
        project_root=project, user_home=user_home
    )

    # Wrote to user-global.
    assert written == str(user_home / ".mcp.json")
    # Project file is byte-identical to what we seeded — git would see
    # no diff. This is the regression test for QM #430.
    assert _read_mcp_json(project / ".mcp.json") == project_original


def test_mcp_bootstrap_preserves_existing_servers_in_user_home(tmp_path, monkeypatch):
    """A user who already has other servers in `~/.mcp.json` must keep
    them — the bootstrap merges tubemail-channel alongside, never
    clobbers."""
    import json as _json
    project = tmp_path / "proj"
    project.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.chdir(project)

    existing = {
        "mcpServers": {
            "other-tool": {"command": "some-other-mcp"},
        },
    }
    (user_home / ".mcp.json").write_text(_json.dumps(existing, indent=2))

    claude_tm._ensure_mcp_channel_entry(
        project_root=project, user_home=user_home
    )

    cfg = _read_mcp_json(user_home / ".mcp.json")
    # Pre-existing user entry survives untouched.
    assert cfg["mcpServers"]["other-tool"] == {"command": "some-other-mcp"}
    # Ours added alongside.
    assert cfg["mcpServers"]["tubemail-channel"] == {"command": "tubemail"}


def test_mcp_bootstrap_skips_when_entry_already_present_in_project(
    tmp_path, monkeypatch
):
    """Idempotency: a project .mcp.json that already lists tubemail-channel
    must not be rewritten — even if the user customised the value."""
    import json as _json
    project = tmp_path / "proj"
    project.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.chdir(project)

    original = {
        "mcpServers": {
            "tubemail-channel": {
                "command": "/custom/path/to/tubemail",
                "args": ["--verbose"],
            },
        },
    }
    (project / ".mcp.json").write_text(_json.dumps(original, indent=2))

    written = claude_tm._ensure_mcp_channel_entry(
        project_root=project, user_home=user_home
    )

    assert written is None
    # File contents unchanged — customised entry preserved verbatim.
    assert _read_mcp_json(project / ".mcp.json") == original


def test_mcp_bootstrap_skips_when_entry_in_user_home(tmp_path, monkeypatch):
    """A user who already has a global ~/.mcp.json entry (Andre's manual
    fix) shouldn't get a duplicate project-local entry — the global one
    already satisfies claude's resolution."""
    import json as _json
    project = tmp_path / "proj"
    project.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.chdir(project)

    (user_home / ".mcp.json").write_text(_json.dumps({
        "mcpServers": {"tubemail-channel": {"command": "tubemail"}},
    }))

    written = claude_tm._ensure_mcp_channel_entry(
        project_root=project, user_home=user_home
    )

    assert written is None
    # No project file was created.
    assert not (project / ".mcp.json").exists()


def test_mcp_bootstrap_respects_skip_env(tmp_path, monkeypatch):
    """Power users who manage .mcp.json themselves can disable the
    auto-registration with TM_SKIP_MCP_BOOTSTRAP=1."""
    project = tmp_path / "proj"
    project.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("TM_SKIP_MCP_BOOTSTRAP", "1")

    written = claude_tm._ensure_mcp_channel_entry(
        project_root=project, user_home=user_home
    )

    assert written is None
    # Neither file gets created when skip is set.
    assert not (project / ".mcp.json").exists()
    assert not (user_home / ".mcp.json").exists()


def test_mcp_bootstrap_does_not_clobber_invalid_json(tmp_path, monkeypatch, capsys):
    """If `~/.mcp.json` exists but isn't parseable, we MUST NOT overwrite
    it — that would destroy whatever the user was editing. Print
    actionable instructions and leave the file intact."""
    project = tmp_path / "proj"
    project.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.chdir(project)

    broken = '{"mcpServers": { unfinished'
    (user_home / ".mcp.json").write_text(broken)

    written = claude_tm._ensure_mcp_channel_entry(
        project_root=project, user_home=user_home
    )

    assert written is None
    # The broken file is preserved byte-for-byte.
    assert (user_home / ".mcp.json").read_text() == broken
    err = capsys.readouterr().err
    assert "tubemail-channel" in err
    assert "manually" in err.lower()


def test_resolve_session_name_env_override_wins(tmp_path, monkeypatch):
    project = tmp_path / "leanspecs"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("TM_WORKER_NAME", "alt")
    # role is ignored when TM_WORKER_NAME is set; the override is the final say.
    assert claude_tm._resolve_session_name("spec") == "alt-tm"


def test_has_continue_flag():
    assert claude_tm._has_continue_flag(["--continue"]) is True
    assert claude_tm._has_continue_flag(["-c"]) is True
    assert claude_tm._has_continue_flag(["-p", "foo"]) is False
    assert claude_tm._has_continue_flag([]) is False


def test_parse_env_file_basic(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "FOO=bar\n"
        "BAZ = qux \n"
        'QUOTED="has spaces"\n'
        "SINGLE='also spaces'\n"
        "EMPTY=\n"
        "NOEQUALS\n"
        "\n"
    )
    parsed = claude_tm._parse_env_file(env)
    assert parsed == {
        "FOO": "bar",
        "BAZ": "qux",
        "QUOTED": "has spaces",
        "SINGLE": "also spaces",
        "EMPTY": "",
    }


def test_parse_env_file_missing_returns_empty(tmp_path: Path):
    assert claude_tm._parse_env_file(tmp_path / "nope") == {}


def test_load_env_files_does_not_overwrite_existing(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TUBEMAIL_SECRET=from-file\nNEW_KEY=fresh\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUBEMAIL_SECRET", "from-shell")
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("TUBEMAIL_ENV_FILE", raising=False)

    import os

    claude_tm._load_env_files()
    assert os.environ["TUBEMAIL_SECRET"] == "from-shell"  # not overwritten
    assert os.environ["NEW_KEY"] == "fresh"  # newly added


def test_find_dotenv_upward_in_cwd(tmp_path: Path):
    (tmp_path / ".env").write_text("X=1\n")
    found = claude_tm._find_dotenv_upward(tmp_path)
    assert found == (tmp_path / ".env").resolve()


def test_find_dotenv_upward_parent(tmp_path: Path):
    (tmp_path / ".env").write_text("X=1\n")
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    found = claude_tm._find_dotenv_upward(sub)
    assert found == (tmp_path / ".env").resolve()


def test_find_dotenv_upward_none(tmp_path: Path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    # No .env anywhere above (tmp_path is under /tmp, which is the parent of
    # all walking; we stop at the filesystem root if no .env appears).
    assert claude_tm._find_dotenv_upward(deep) is None


def test_load_env_files_walks_up_to_repo_root(tmp_path: Path, monkeypatch):
    """Monorepo case: .env at repo root, claude-tm launched from a subdir."""
    repo_root = tmp_path / "repo"
    sub = repo_root / "project"
    sub.mkdir(parents=True)
    (repo_root / ".env").write_text("TUBEMAIL_SECRET=from-repo-root\n")
    monkeypatch.chdir(sub)
    monkeypatch.delenv("TUBEMAIL_SECRET", raising=False)
    monkeypatch.delenv("TUBEMAIL_ENV_FILE", raising=False)

    import os

    claude_tm._load_env_files()
    assert os.environ["TUBEMAIL_SECRET"] == "from-repo-root"


def test_load_env_files_explicit_path_wins(tmp_path: Path, monkeypatch):
    """TUBEMAIL_ENV_FILE wins per key, and lower layers still fill the gaps."""
    explicit = tmp_path / "custom.env"
    explicit.write_text("SHARED=from-explicit\n")
    cwd_env = tmp_path / ".env"
    cwd_env.write_text("SHARED=from-cwd\nFROM_CWD=yes\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUBEMAIL_ENV_FILE", str(explicit))
    monkeypatch.delenv("SHARED", raising=False)
    monkeypatch.delenv("FROM_CWD", raising=False)

    import os

    claude_tm._load_env_files()
    # Precedence: the explicit file is consulted first, so its value stands.
    assert os.environ["SHARED"] == "from-explicit"
    # Layering: keys the explicit file does not define still come from cwd.
    assert os.environ["FROM_CWD"] == "yes"


def test_main_help_short_circuits(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["claude-tm", "--help"])
    claude_tm.main()
    out = capsys.readouterr().out
    assert "claude-tm" in out
    assert "TUBEMAIL_SECRET" in out


def test_check_pidfile_no_file_returns(tmp_path: Path, monkeypatch):
    # Point pidfile resolution at a tmp directory the test owns.
    monkeypatch.setattr(claude_tm, "Path", Path)
    # The default path is /tmp/claude-tm-<name>.pid. Use a session name
    # that is guaranteed not to collide with any real pidfile.
    name = "test-no-such-pidfile-xyz-123"
    pidfile = Path(f"/tmp/claude-tm-{name}.pid")
    pidfile.unlink(missing_ok=True)
    # Should not raise.
    claude_tm._check_pidfile(name)


def test_check_pidfile_dead_pid_returns(tmp_path: Path, monkeypatch):
    name = "test-dead-pidfile-xyz-123"
    pidfile = Path(f"/tmp/claude-tm-{name}.pid")
    # PID 999999 is overwhelmingly likely to be unused on Linux.
    pidfile.write_text("999999")
    monkeypatch.setattr(claude_tm, "_pid_alive", lambda pid: False)
    try:
        claude_tm._check_pidfile(name)
    finally:
        pidfile.unlink(missing_ok=True)


def test_check_pidfile_live_pid_exits(monkeypatch, capsys):
    name = "test-live-pidfile-xyz-123"
    pidfile = Path(f"/tmp/claude-tm-{name}.pid")
    pidfile.write_text("123")
    monkeypatch.setattr(claude_tm, "_pid_alive", lambda pid: True)
    monkeypatch.delenv("TM_FORCE", raising=False)
    try:
        with pytest.raises(SystemExit) as exc:
            claude_tm._check_pidfile(name)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "already running as pid 123" in err
    finally:
        pidfile.unlink(missing_ok=True)


def test_check_pidfile_live_pid_with_force_continues(monkeypatch, capsys):
    name = "test-live-force-xyz-123"
    pidfile = Path(f"/tmp/claude-tm-{name}.pid")
    pidfile.write_text("123")
    monkeypatch.setattr(claude_tm, "_pid_alive", lambda pid: True)
    monkeypatch.setenv("TM_FORCE", "1")
    try:
        claude_tm._check_pidfile(name)  # should not raise
        err = capsys.readouterr().err
        assert "TM_FORCE=1" in err
    finally:
        pidfile.unlink(missing_ok=True)


def test_load_env_files_falls_back_to_global_when_local_env_lacks_secret(
    tmp_path: Path, monkeypatch
):
    """A project .env that does not define TUBEMAIL_SECRET must not shadow
    ~/.config/tubemail/.env.

    Regression: the candidate loop returned after the first *existing* file
    rather than layering, so any repo carrying its own unrelated .env (e.g. a
    Vite frontend) made the global fallback unreachable and claude-tm died
    with "TUBEMAIL_SECRET is not set".
    """
    home = tmp_path / "home"
    project = home / "projects" / "some-frontend"
    project.mkdir(parents=True)
    (project / ".env").write_text("VITE_AWS_REGION=us-east-1\n")
    global_env = home / ".config" / "tubemail"
    global_env.mkdir(parents=True)
    (global_env / ".env").write_text("TUBEMAIL_SECRET=from-global\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    monkeypatch.delenv("TUBEMAIL_SECRET", raising=False)
    monkeypatch.delenv("VITE_AWS_REGION", raising=False)
    monkeypatch.delenv("TUBEMAIL_ENV_FILE", raising=False)

    import os

    claude_tm._load_env_files()
    assert os.environ["VITE_AWS_REGION"] == "us-east-1"  # local .env still read
    assert os.environ["TUBEMAIL_SECRET"] == "from-global"  # global fills the gap


def test_load_env_files_local_env_wins_over_global_per_key(tmp_path: Path, monkeypatch):
    """Layering is per-key first-wins: the nearer file still beats the global."""
    home = tmp_path / "home"
    project = home / "projects" / "worker"
    project.mkdir(parents=True)
    (project / ".env").write_text("TUBEMAIL_SECRET=from-project\n")
    global_env = home / ".config" / "tubemail"
    global_env.mkdir(parents=True)
    (global_env / ".env").write_text("TUBEMAIL_SECRET=from-global\nONLY_GLOBAL=yes\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    monkeypatch.delenv("TUBEMAIL_SECRET", raising=False)
    monkeypatch.delenv("ONLY_GLOBAL", raising=False)
    monkeypatch.delenv("TUBEMAIL_ENV_FILE", raising=False)

    import os

    claude_tm._load_env_files()
    assert os.environ["TUBEMAIL_SECRET"] == "from-project"
    assert os.environ["ONLY_GLOBAL"] == "yes"
