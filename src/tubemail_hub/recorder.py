"""Text-screen recorder for worker pty streams.

Tees raw pty bytes to two parallel files per active session:

  <data_dir>/recordings/<worker>/<session_ts>.cast        — asciinema v2
  <data_dir>/recordings/<worker>/<session_ts>.frames.jsonl — ANSI-stripped text frames

The .cast file is the source of truth — full fidelity, replayable with
`asciinema play`. The .frames.jsonl file is what the orchestrator reads via
`tm_get_recording` — one JSON object per write, ANSI escapes stripped, easy
to grep and slice by time.

A "session" is a continuous span where recording is enabled for one worker.
Toggling off→on starts a new session. Files rotate when the active .cast
exceeds `max_bytes_per_file`; rotation closes the current session and opens
a fresh one. Older files are GC'd so total per-worker bytes stays under
`max_bytes_per_file * keep_files`.

Disk I/O is synchronous in the hot path (small pty chunks, append-only
writes are fast). If profiling later shows a bottleneck, swap to a queue
+ background drainer.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


# ANSI CSI / OSC / single-char escapes. Same shape as the manager's screenshot
# stripper — we strip on the hub side because the hub is what writes
# frames.jsonl, and we want the file readable without a vt100 emulator.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")


def strip_ansi(data: bytes) -> str:
    """Strip ANSI escapes and return UTF-8 text. Replace bad bytes."""
    return _ANSI_RE.sub(b"", data).decode("utf-8", errors="replace")


def _ts_filename() -> str:
    """UTC timestamp safe for filenames (no colons, no spaces).

    Microsecond precision so rapid rotations within the same second don't
    collide on filename and overwrite each other.
    """
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S") + f"-{now.microsecond:06d}Z"


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _iter_frames(path: Path) -> Iterator[dict[str, Any]]:
    """Yield frame entries from a .frames.jsonl file, skipping malformed lines."""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # A truncated last line during an active write is the
                    # only way this happens. Skip and continue.
                    continue
    except OSError as e:
        logger.warning("recorder: read failed for %s: %s", path, e)
        return


def _read_frames_in_dir(
    directory: Path,
    *,
    since: str | None,
    until: str | None,
    grep: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Read frames across every .frames.jsonl in a directory in chronological
    order, filtered by time range and optional regex."""
    pattern = re.compile(grep) if grep else None
    frames_files = sorted(directory.glob("*.frames.jsonl"))
    out: list[dict[str, Any]] = []
    for path in frames_files:
        for entry in _iter_frames(path):
            t = entry.get("t", "")
            if since and t < since:
                continue
            if until and t >= until:
                continue
            if pattern is not None and not pattern.search(entry.get("delta", "")):
                continue
            out.append(entry)
            if len(out) >= limit:
                return out
    return out


class WorkerRecording:
    """Owns the active .cast + .frames.jsonl file pair for one worker.

    Not thread-safe. The RecordingManager calls into this from the asyncio
    event loop so all writes are serialized by the loop. Don't share across
    workers.
    """

    def __init__(
        self,
        worker: str,
        directory: Path,
        *,
        max_bytes_per_file: int,
        keep_files: int,
    ) -> None:
        self.worker = worker
        self.directory = directory
        self.max_bytes_per_file = max_bytes_per_file
        self.keep_files = keep_files
        self.directory.mkdir(parents=True, exist_ok=True)
        self._session_start: float | None = None
        self._cast_path: Path | None = None
        self._frames_path: Path | None = None
        self._cast_size: int = 0
        self._frames_size: int = 0
        self._open_session()

    # ── session lifecycle ────────────────────────────────────────────────

    def _open_session(self) -> None:
        """Open a fresh .cast + .frames.jsonl pair."""
        self._session_start = time.time()
        stem = _ts_filename()
        self._cast_path = self.directory / f"{stem}.cast"
        self._frames_path = self.directory / f"{stem}.frames.jsonl"
        # Asciinema v2 header. Width/height are best-effort defaults; the
        # cast plays back fine even if they don't match the original tty.
        header = {
            "version": 2,
            "width": 200,
            "height": 50,
            "timestamp": int(self._session_start),
            "title": f"tubemail recording: {self.worker}",
        }
        line = json.dumps(header) + "\n"
        with self._cast_path.open("w", encoding="utf-8") as f:
            f.write(line)
        self._cast_size = len(line.encode("utf-8"))
        self._frames_size = 0
        # Touch the frames file so consumers see an empty-but-present file
        # immediately after recording starts.
        self._frames_path.touch()
        self._gc_old_files()

    def close(self) -> None:
        """Close the current session. Writes are no-ops after this."""
        self._cast_path = None
        self._frames_path = None
        self._session_start = None

    @property
    def active(self) -> bool:
        return self._cast_path is not None

    # ── writes ───────────────────────────────────────────────────────────

    def write(self, data: bytes) -> None:
        """Append a chunk of pty output to both files."""
        if not data or self._cast_path is None or self._frames_path is None:
            return
        if self._session_start is None:
            return
        delay = max(0.0, time.time() - self._session_start)
        # asciinema v2 line: [delay_seconds, "o", "data"]
        cast_line = json.dumps([round(delay, 6), "o", data.decode("utf-8", errors="replace")]) + "\n"
        frame_line = json.dumps({"t": _iso_now(), "delta": strip_ansi(data)}) + "\n"
        try:
            with self._cast_path.open("a", encoding="utf-8") as f:
                f.write(cast_line)
            with self._frames_path.open("a", encoding="utf-8") as f:
                f.write(frame_line)
        except OSError as e:
            # Recording must never break the bridge — log and drop the chunk.
            # The user will see a gap in the file, not a hung session.
            logger.warning(
                "recorder: write failed for %s: %s — dropping chunk",
                self.worker, e,
            )
            return
        self._cast_size += len(cast_line.encode("utf-8"))
        self._frames_size += len(frame_line.encode("utf-8"))
        if (
            self._cast_size >= self.max_bytes_per_file
            or self._frames_size >= self.max_bytes_per_file
        ):
            self._rotate()

    def _rotate(self) -> None:
        """Start a new session (new file pair). The previous session is
        flushed to disk and remains on disk until GC drops it."""
        self.close()
        self._open_session()

    # ── GC ───────────────────────────────────────────────────────────────

    def _gc_old_files(self) -> None:
        """Drop oldest files so the worker dir holds at most `keep_files`
        cast files (and their frame siblings)."""
        casts = sorted(
            self.directory.glob("*.cast"),
            key=lambda p: p.stat().st_mtime,
        )
        # Keep the newest `keep_files`. We just opened a new file so the
        # current session counts toward the budget.
        excess = len(casts) - self.keep_files
        if excess <= 0:
            return
        for old_cast in casts[:excess]:
            stem = old_cast.stem
            old_frames = self.directory / f"{stem}.frames.jsonl"
            try:
                old_cast.unlink()
            except OSError as e:
                logger.warning("recorder: failed to remove %s: %s", old_cast, e)
            if old_frames.exists():
                try:
                    old_frames.unlink()
                except OSError as e:
                    logger.warning("recorder: failed to remove %s: %s", old_frames, e)

    # ── reads (for tm_get_recording) ─────────────────────────────────────

    def list_files(self) -> list[dict[str, Any]]:
        """Return metadata for every recording file pair on disk for this
        worker, newest first."""
        casts = sorted(
            self.directory.glob("*.cast"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out = []
        for cast in casts:
            frames = self.directory / f"{cast.stem}.frames.jsonl"
            cast_stat = cast.stat()
            entry: dict[str, Any] = {
                "stem": cast.stem,
                "cast_path": str(cast),
                "frames_path": str(frames) if frames.exists() else None,
                "size_bytes": cast_stat.st_size + (
                    frames.stat().st_size if frames.exists() else 0
                ),
                "modified": cast_stat.st_mtime,
                "active": (self._cast_path is not None and cast == self._cast_path),
            }
            out.append(entry)
        return out

    def read_frames(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        grep: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Read frames across all on-disk frame files for this worker, in
        chronological order, filtered by time range and optional regex.

        `since` / `until` are ISO-8601 strings (inclusive lower, exclusive
        upper). `grep` is a regex matched against the `delta` field. `limit`
        caps the returned count — callers page by tightening `since`.
        """
        return _read_frames_in_dir(
            self.directory, since=since, until=until, grep=grep, limit=limit,
        )


class RecordingManager:
    """Owns one WorkerRecording per worker that has recording enabled.

    Wired into the PtyBridgeRegistry as a tee target so `push_output`
    fans bytes to the active recording without going through the browser
    fan-out.
    """

    def __init__(
        self,
        recordings_dir: Path,
        *,
        max_bytes_per_file: int = 5 * 1024 * 1024,
        keep_files: int = 4,
    ) -> None:
        self._dir = Path(recordings_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes_per_file = max_bytes_per_file
        self.keep_files = keep_files
        self._active: dict[str, WorkerRecording] = {}

    # ── runtime knobs (settings page) ────────────────────────────────────

    def update_limits(
        self, *, max_bytes_per_file: int | None = None, keep_files: int | None = None
    ) -> None:
        """Apply new size/keep limits live. Existing recordings pick up the
        new max on next rotation; new recordings see them immediately."""
        if max_bytes_per_file is not None and max_bytes_per_file > 0:
            self.max_bytes_per_file = max_bytes_per_file
            for rec in self._active.values():
                rec.max_bytes_per_file = max_bytes_per_file
        if keep_files is not None and keep_files > 0:
            self.keep_files = keep_files
            for rec in self._active.values():
                rec.keep_files = keep_files

    # ── per-worker control ───────────────────────────────────────────────

    def is_recording(self, worker: str) -> bool:
        return worker in self._active

    def start(self, worker: str) -> dict[str, Any]:
        """Begin (or resume) recording for this worker. Idempotent."""
        existing = self._active.get(worker)
        if existing is not None and existing.active:
            return self.status(worker)
        rec = WorkerRecording(
            worker=worker,
            directory=self._worker_dir(worker),
            max_bytes_per_file=self.max_bytes_per_file,
            keep_files=self.keep_files,
        )
        self._active[worker] = rec
        logger.info("recorder: started recording for %s", worker)
        return self.status(worker)

    def stop(self, worker: str) -> dict[str, Any]:
        rec = self._active.pop(worker, None)
        if rec is not None:
            rec.close()
            logger.info("recorder: stopped recording for %s", worker)
        return self.status(worker)

    def write(self, worker: str, data: bytes) -> None:
        rec = self._active.get(worker)
        if rec is None:
            return
        rec.write(data)

    # ── reads / status ───────────────────────────────────────────────────

    def status(self, worker: str) -> dict[str, Any]:
        rec = self._active.get(worker)
        files = self._list_files_for(worker)
        return {
            "worker": worker,
            "enabled": rec is not None,
            "active_file": rec._cast_path.name if rec and rec._cast_path else None,
            "files": files,
            "max_bytes_per_file": self.max_bytes_per_file,
            "keep_files": self.keep_files,
        }

    def list_files(self, worker: str) -> list[dict[str, Any]]:
        return self._list_files_for(worker)

    def read_frames(
        self,
        worker: str,
        *,
        since: str | None = None,
        until: str | None = None,
        grep: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        wd = self._worker_dir(worker)
        if not wd.exists():
            return []
        return _read_frames_in_dir(
            wd, since=since, until=until, grep=grep, limit=limit,
        )

    def purge_worker(self, worker: str) -> int:
        """Delete every recording file for a worker. Returns count removed."""
        self.stop(worker)
        wd = self._worker_dir(worker)
        if not wd.exists():
            return 0
        n = 0
        for p in wd.iterdir():
            try:
                p.unlink()
                n += 1
            except OSError as e:
                logger.warning("recorder: purge failed for %s: %s", p, e)
        try:
            wd.rmdir()
        except OSError:
            pass
        return n

    # ── helpers ──────────────────────────────────────────────────────────

    def _worker_dir(self, worker: str) -> Path:
        # Worker name validation already happens upstream (BridgeEngine
        # enforces the regex). Defense-in-depth: refuse anything with a
        # path separator or `..` so a future careless caller can't write
        # outside our recordings dir.
        if "/" in worker or "\\" in worker or ".." in worker:
            raise ValueError(f"invalid worker name for recording dir: {worker!r}")
        return self._dir / worker

    def _list_files_for(self, worker: str) -> list[dict[str, Any]]:
        rec = self._active.get(worker)
        if rec is not None:
            return rec.list_files()
        wd = self._worker_dir(worker)
        if not wd.exists():
            return []
        casts = sorted(
            wd.glob("*.cast"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out = []
        for cast in casts:
            frames = wd / f"{cast.stem}.frames.jsonl"
            cast_stat = cast.stat()
            out.append({
                "stem": cast.stem,
                "cast_path": str(cast),
                "frames_path": str(frames) if frames.exists() else None,
                "size_bytes": cast_stat.st_size + (
                    frames.stat().st_size if frames.exists() else 0
                ),
                "modified": cast_stat.st_mtime,
                "active": False,
            })
        return out


__all__ = ["RecordingManager", "WorkerRecording", "strip_ansi"]
