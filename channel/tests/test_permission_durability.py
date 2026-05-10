"""Tests for permission_response retry+spool durability.

Covers the four real-world paths:
  1. Happy: hub accepts on first try → no spool, no retries.
  2. Transient: hub fails N times then accepts → no spool.
  3. Hard outage: hub never accepts → entry lands in spool intact.
  4. Recovery: spool exists on startup → drain forwards in order.

The Stop hook's spool was added under #205 after dogfooding the same
class of bug; this module mirrors the design at the channel layer for
permission_response. These tests pin both the retry chain and the
spool format so a future refactor can't silently break either.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tubemail.permission_durability import (
    PermissionResponseSpool,
    SPOOL_ROOT_ENV,
    drain_spool,
    post_permission_response_durable,
)


# ── test doubles ────────────────────────────────────────────────────────


class FakeHub:
    """Records every post_permission_response call. fail_first lets a
    test demand N failures before acceptance — covers the retry-then-
    success path without needing real network flakiness."""

    def __init__(self, fail_first: int = 0, fail_forever: bool = False) -> None:
        self.fail_first = fail_first
        self.fail_forever = fail_forever
        self.calls: list[tuple[str, str]] = []

    async def post_permission_response(self, request_id: str, behavior: str):
        self.calls.append((request_id, behavior))
        if self.fail_forever:
            raise httpx.ConnectError("hub down forever")
        if self.fail_first > 0:
            self.fail_first -= 1
            raise httpx.ConnectError("transient")
        return {"event_id": "fake"}


@pytest.fixture
def spool_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv(SPOOL_ROOT_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def spool(spool_root) -> PermissionResponseSpool:
    return PermissionResponseSpool("leanspecs-code-tm")


async def _no_sleep(_):
    """Pass into post_permission_response_durable so the retry chain
    runs at zero real-world latency — tests stay fast and deterministic."""


# ── PermissionResponseSpool ─────────────────────────────────────────────


def test_spool_root_honors_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(SPOOL_ROOT_ENV, str(tmp_path / "custom"))
    s = PermissionResponseSpool("w")
    assert s.directory == tmp_path / "custom" / "w"


def test_spool_root_isolates_per_worker(spool_root):
    a = PermissionResponseSpool("worker-a")
    b = PermissionResponseSpool("worker-b")
    assert a.directory != b.directory
    assert a.directory.parent == b.directory.parent


def test_spool_write_persists_payload_with_secure_mode(spool):
    assert spool.write("rid42", "allow") is True
    files = spool.list_pending()
    assert len(files) == 1
    f = files[0]
    assert f.name.startswith("permission-")
    assert f.name.endswith(".json")
    body = json.loads(f.read_text())
    assert body["request_id"] == "rid42"
    assert body["behavior"] == "allow"
    assert "spooled_at" in body
    # Owner-only mode prevents another local user from reading
    # request_ids out of the spool. mkstemp+rename can leave wider
    # perms; the explicit chmod 0600 closes the gap.
    assert (f.stat().st_mode & 0o777) == 0o600


def test_spool_write_sanitizes_request_id_in_filename(spool):
    """A malicious or weird request_id must not influence the spool path
    — the channel passes whatever Claude gave it, and we don't trust the
    client to bound the character set."""
    assert spool.write("../../etc/passwd", "deny") is True
    files = spool.list_pending()
    assert len(files) == 1
    # The unsafe characters got replaced by underscores in the filename
    # but the original string is preserved in the JSON body for fidelity.
    assert "../" not in files[0].name
    assert "etc" in files[0].name or "_etc_" in files[0].name
    body = json.loads(files[0].read_text())
    assert body["request_id"] == "../../etc/passwd"


def test_list_pending_returns_oldest_first(spool):
    spool.write("first", "allow")
    spool.write("second", "deny")
    spool.write("third", "allow")
    files = spool.list_pending()
    rids = [json.loads(f.read_text())["request_id"] for f in files]
    assert rids == ["first", "second", "third"]


def test_list_pending_on_missing_directory_returns_empty(spool):
    # No writes → directory was never created.
    assert spool.list_pending() == []


# ── post_permission_response_durable ────────────────────────────────────


async def test_durable_post_returns_true_on_first_try(spool):
    hub = FakeHub()
    ok = await post_permission_response_durable(
        hub, spool, "rid", "allow", sleep_fn=_no_sleep,
    )
    assert ok is True
    assert hub.calls == [("rid", "allow")]
    assert spool.list_pending() == []


async def test_durable_post_retries_on_transient_failure(spool):
    """Two failures then success: must return True without spooling and
    must have called the hub three times total. The retry chain bounds
    are pinned in the module — a refactor that drops retries would
    silently regress this test."""
    hub = FakeHub(fail_first=2)
    ok = await post_permission_response_durable(
        hub, spool, "rid", "deny", sleep_fn=_no_sleep,
    )
    assert ok is True
    assert len(hub.calls) == 3
    assert spool.list_pending() == []


async def test_durable_post_spools_when_all_retries_fail(spool):
    """Hub down for all attempts: returns False, entry lands in spool
    with full fidelity. The next successful post (or channel restart)
    will drain it."""
    hub = FakeHub(fail_forever=True)
    ok = await post_permission_response_durable(
        hub, spool, "rid-stuck", "allow", sleep_fn=_no_sleep,
    )
    assert ok is False
    files = spool.list_pending()
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["request_id"] == "rid-stuck"
    assert body["behavior"] == "allow"


async def test_durable_post_does_not_spool_unrelated_exceptions(
    spool, monkeypatch,
):
    """Bugs in the call path (TypeError, AttributeError) must propagate
    — the spool is for transport-layer failures only, not for hiding
    programmer mistakes that would otherwise be loud."""

    class BuggyHub:
        async def post_permission_response(self, request_id, behavior):
            raise TypeError("you passed the wrong shape")

    with pytest.raises(TypeError):
        await post_permission_response_durable(
            BuggyHub(), spool, "rid", "allow", sleep_fn=_no_sleep,
        )
    assert spool.list_pending() == []


# ── drain_spool ─────────────────────────────────────────────────────────


async def test_drain_spool_forwards_in_order(spool):
    """Spool drains oldest-first so the hub sees resolutions in the
    same order the user actually answered them."""
    spool.write("first", "allow")
    spool.write("second", "deny")
    spool.write("third", "allow")
    hub = FakeHub()
    drained = await drain_spool(hub, spool, sleep_fn=_no_sleep)
    assert drained == 3
    assert hub.calls == [
        ("first", "allow"),
        ("second", "deny"),
        ("third", "allow"),
    ]
    assert spool.list_pending() == []


async def test_drain_spool_stops_on_first_failure(spool):
    """If the hub starts rejecting partway through the drain, the
    remaining entries stay on disk for the next try. Otherwise we'd
    burn the full retry chain on every entry while the hub is still
    down — and lose entries to the spool cap if the outage runs long."""
    spool.write("first", "allow")
    spool.write("second", "allow")
    spool.write("third", "allow")
    hub = FakeHub(fail_first=10)  # always fails for this test scope
    drained = await drain_spool(hub, spool, sleep_fn=_no_sleep)
    assert drained == 0
    # All three still in spool — none dropped on a transport failure.
    assert len(spool.list_pending()) == 3


async def test_drain_spool_skips_malformed_entries(spool):
    """A truncated or hand-edited spool entry must not poison the drain
    — log it, drop it from disk, continue with the rest."""
    # Drop a malformed file directly into the spool dir.
    spool.write("good", "allow")
    bad = spool.directory / "permission-1234-bad.json"
    bad.write_text("{not json")
    hub = FakeHub()
    drained = await drain_spool(hub, spool, sleep_fn=_no_sleep)
    assert drained == 1
    assert hub.calls == [("good", "allow")]
    assert not bad.exists()


async def test_drain_spool_skips_entries_with_invalid_behavior(spool):
    """Defense in depth: a hand-crafted spool entry with behavior=
    something other than allow/deny must not get forwarded — it would
    fail the hub's schema check anyway, but skipping locally avoids
    burning a retry chain."""
    bogus = spool.directory
    bogus.mkdir(parents=True, exist_ok=True)
    (bogus / "permission-9999-x.json").write_text(
        json.dumps({"request_id": "x", "behavior": "maybe"}),
    )
    hub = FakeHub()
    drained = await drain_spool(hub, spool, sleep_fn=_no_sleep)
    assert drained == 0
    assert hub.calls == []
    # The malformed entry was dropped.
    assert spool.list_pending() == []
