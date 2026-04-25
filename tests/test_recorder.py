"""Tests for the pty recording subsystem."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tubemail_hub.recorder import RecordingManager, strip_ansi


def test_strip_ansi_removes_csi_and_osc():
    raw = b"hello\x1b[31m world\x1b[0m\x1b]0;title\x07!"
    assert strip_ansi(raw) == "hello world!"


def test_start_writes_cast_header_and_empty_frames(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    rm.start("alice")
    files = rm.list_files("alice")
    assert len(files) == 1
    cast = Path(files[0]["cast_path"])
    frames = Path(files[0]["frames_path"])
    assert cast.exists() and frames.exists()
    header = json.loads(cast.read_text().splitlines()[0])
    assert header["version"] == 2
    assert header["title"].endswith("alice")
    # frames.jsonl exists but is empty until the first write
    assert frames.read_text() == ""


def test_write_appends_to_both_files(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    rm.start("alice")
    rm.write("alice", b"hello\x1b[31m world\x1b[0m")
    files = rm.list_files("alice")
    cast_lines = Path(files[0]["cast_path"]).read_text().splitlines()
    frame_lines = Path(files[0]["frames_path"]).read_text().splitlines()
    # Header + one event line
    assert len(cast_lines) == 2
    event = json.loads(cast_lines[1])
    assert event[1] == "o"
    assert "hello" in event[2]
    # Frame line has stripped delta
    assert len(frame_lines) == 1
    frame = json.loads(frame_lines[0])
    assert frame["delta"] == "hello world"
    assert "t" in frame


def test_write_when_not_started_is_noop(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    # No start — write should silently do nothing, not crash.
    rm.write("alice", b"data")
    assert rm.list_files("alice") == []


def test_rotation_creates_new_file_on_size_threshold(tmp_path: Path):
    # Tiny threshold so a single write rotates.
    rm = RecordingManager(
        tmp_path / "rec", max_bytes_per_file=200, keep_files=4,
    )
    rm.start("alice")
    # Write enough to push past the threshold across two writes.
    rm.write("alice", b"X" * 150)
    rm.write("alice", b"Y" * 150)
    files = rm.list_files("alice")
    assert len(files) >= 2, f"expected rotation, got {files}"


def test_keep_files_drops_oldest(tmp_path: Path):
    rm = RecordingManager(
        tmp_path / "rec", max_bytes_per_file=80, keep_files=2,
    )
    rm.start("alice")
    # Each write rotates; keep_files=2 means after several rotations only
    # the two most recent files remain.
    for i in range(6):
        rm.write("alice", b"X" * 100)
        # Tiny sleep so the mtime ordering is stable.
        time.sleep(0.01)
    files = rm.list_files("alice")
    assert len(files) <= 2


def test_stop_then_start_creates_separate_session(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    rm.start("alice")
    rm.write("alice", b"first")
    rm.stop("alice")
    rm.start("alice")
    rm.write("alice", b"second")
    files = rm.list_files("alice")
    assert len(files) == 2


def test_read_frames_filters_by_grep(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    rm.start("alice")
    rm.write("alice", b"hello world\n")
    rm.write("alice", b"goodbye world\n")
    rm.write("alice", b"hello again\n")
    matches = rm.read_frames("alice", grep="hello")
    assert len(matches) == 2
    assert all("hello" in m["delta"] for m in matches)


def test_read_frames_filters_by_time_range(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    rm.start("alice")
    rm.write("alice", b"first")
    # Capture a fence timestamp
    fence = rm.read_frames("alice")[-1]["t"]
    time.sleep(0.05)
    rm.write("alice", b"second")
    after = rm.read_frames("alice", since=fence)
    # `since` is inclusive — the fence frame plus the new one. Just assert
    # the post-fence frame is included; second frame should be present.
    assert any("second" in f["delta"] for f in after)


def test_read_frames_limit_and_truncation(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    rm.start("alice")
    for i in range(20):
        rm.write("alice", f"chunk-{i}\n".encode())
    page = rm.read_frames("alice", limit=5)
    assert len(page) == 5
    # Chronological order: oldest first.
    assert "chunk-0" in page[0]["delta"]


def test_purge_worker_removes_files(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    rm.start("alice")
    rm.write("alice", b"hello")
    n = rm.purge_worker("alice")
    assert n >= 2  # cast + frames at minimum
    assert rm.list_files("alice") == []


def test_invalid_worker_name_rejected(tmp_path: Path):
    rm = RecordingManager(tmp_path / "rec")
    with pytest.raises(ValueError):
        rm.start("../escape")


def test_update_limits_applies_to_active_recording(tmp_path: Path):
    rm = RecordingManager(
        tmp_path / "rec", max_bytes_per_file=10_000, keep_files=4,
    )
    rm.start("alice")
    rm.update_limits(max_bytes_per_file=200, keep_files=2)
    # Large write should now trigger rotation under the new threshold.
    rm.write("alice", b"X" * 500)
    files = rm.list_files("alice")
    assert len(files) >= 2
