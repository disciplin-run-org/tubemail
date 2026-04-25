"""Hub-wide configuration persisted to JSON.

Lives at `<data_dir>/hub-config.json`. Holds the global defaults and limits
that the web UI's /settings page lets the operator tune at runtime —
currently just the recording knobs, but designed so future settings drop
in here too.

Atomic save: write to a `.tmp` sibling, then `os.replace`. A crash mid-save
leaves either the old file or the new file, never a half-written one.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class HubConfig(BaseModel):
    """Operator-tunable hub settings."""

    # Recording: global default for new workers + size-rotation knobs.
    recording_enabled_by_default: bool = Field(
        default=False,
        description=(
            "If true, every newly registered worker starts with recording on. "
            "Existing workers keep whatever per-worker setting they already have."
        ),
    )
    recording_max_bytes_per_file: int = Field(
        default=5 * 1024 * 1024,  # 5 MiB
        ge=64 * 1024,
        description="Rotate recording files when active file exceeds this size.",
    )
    recording_keep_files: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Per-worker cap on how many recording files survive GC.",
    )


def _config_path(data_dir: Path) -> Path:
    return Path(data_dir) / "hub-config.json"


def load_config(data_dir: Path) -> HubConfig:
    """Load config from disk; return defaults on missing/corrupt file."""
    path = _config_path(data_dir)
    if not path.exists():
        return HubConfig()
    try:
        return HubConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        # A corrupt file is a signal — log it loud and fall back to defaults
        # so the hub still boots. Operator can fix or delete the file.
        logger.warning("hub-config: failed to parse %s: %s — using defaults", path, e)
        return HubConfig()


def save_config(data_dir: Path, cfg: HubConfig) -> None:
    """Atomic write of the full config to disk."""
    path = _config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


__all__ = ["HubConfig", "load_config", "save_config"]
