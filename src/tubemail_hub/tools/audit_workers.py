"""CLI: scan /data/tubemail/workers/*.json for suspicious event-log shape.

A worker that's been active for hours but has only a handful of events
on disk is the QM #187 smoking-gun shape — silent history loss from a
mutating code path that didn't load disk state before persisting fresh.
This tool gives ops a one-liner to spot it after any hub restart or
after a regression that re-introduces the class of bug.

Usage:

    python -m tubemail_hub.tools.audit_workers
    python -m tubemail_hub.tools.audit_workers --workers-dir /custom/path
    python -m tubemail_hub.tools.audit_workers --threshold 0.5

Exit code 0 if no anomalies, non-zero if any worker is flagged. Suitable
for cron / health-check invocation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_WORKERS_DIR = Path("/data/tubemail/workers")
# Below this many events per active hour, flag the worker. A worker
# replying every 36 seconds clears 100/hr trivially — anything below
# ~1 event per active hour is implausible for a working session.
DEFAULT_MIN_EVENTS_PER_ACTIVE_HOUR = 1.0
# Workers active for less than this many seconds are exempt — a freshly-
# started session may have one inbound and no outbound yet.
GRACE_ACTIVE_SECONDS = 300


def _audit_one(data: dict[str, Any], threshold: float) -> dict[str, Any] | None:
    """Return an anomaly dict if `data` looks suspicious, else None."""
    name = data.get("name")
    registered_at = data.get("registered_at") or 0.0
    last_activity = data.get("last_activity") or registered_at
    events = data.get("events") or []
    active_seconds = last_activity - registered_at

    if active_seconds < GRACE_ACTIVE_SECONDS:
        return None
    #end if

    active_hours = max(active_seconds / 3600.0, 1 / 3600.0)
    rate = len(events) / active_hours

    if rate >= threshold:
        return None
    #end if

    return {
        "name": name,
        "events": len(events),
        "active_seconds": int(active_seconds),
        "events_per_hour": round(rate, 3),
        "registered_at": registered_at,
        "last_activity": last_activity,
    }


def audit_workers(
    workers_dir: Path | str,
    *,
    threshold: float = DEFAULT_MIN_EVENTS_PER_ACTIVE_HOUR,
) -> list[dict[str, Any]]:
    """Scan the workers dir and return a list of anomaly dicts. Empty
    list means everything looks healthy. Importable from tests."""
    workers_dir = Path(workers_dir)
    out: list[dict[str, Any]] = []
    if not workers_dir.is_dir():
        return out
    #end if
    for path in sorted(workers_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            out.append({
                "name": path.stem,
                "error": f"failed to read {path.name}: {err}",
            })
            continue
        #end try
        anomaly = _audit_one(data, threshold)
        if anomaly is not None:
            out.append(anomaly)
        #end if
    #end for
    return out


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument(
        "--workers-dir",
        type=Path,
        default=DEFAULT_WORKERS_DIR,
        help="Directory containing <worker>.json state files",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_MIN_EVENTS_PER_ACTIVE_HOUR,
        help="Minimum events per active hour; below this flags an anomaly",
    )
    args = parser.parse_args(argv)

    anomalies = audit_workers(args.workers_dir, threshold=args.threshold)
    if not anomalies:
        print(f"OK: no anomalies in {args.workers_dir} "
              f"(threshold={args.threshold} events/hr)")
        return 0
    #end if

    print(f"FLAGGED {len(anomalies)} worker(s) in {args.workers_dir} "
          f"(threshold={args.threshold} events/hr):")
    for a in anomalies:
        if "error" in a:
            print(f"  [{a['name']}] error: {a['error']}")
            continue
        #end if
        active_hr = a["active_seconds"] / 3600.0
        print(
            f"  [{a['name']}] events={a['events']} "
            f"active={active_hr:.1f}h rate={a['events_per_hour']}/hr"
        )
    #end for
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
