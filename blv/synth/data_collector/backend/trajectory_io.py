"""Pure-Python I/O helpers for trajectory JSON files.

Splitting these out of :mod:`trajectory` keeps the disk-facing side of
the trajectory subsystem importable without ``omni`` or ``carb`` — which
matters because the collect-all planner and the CLI ``list`` command
read trajectories on disk without booting a simulator.

JSON schema (v1.0)
------------------
::

    {
        "version": "1.0",
        "name": "trajectory_001",
        "environment": "hospital_hallway",
        "camera_path": "/World/BLV_Camera",
        "fps": 60,
        "frame_count": 300,
        "created": "2026-04-06T13:00:00",
        "frames": [
            {"frame": 0, "position": [x, y, z], "rotation": [pitch, yaw, roll]},
            ...
        ]
    }
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

try:
    import carb  # type: ignore

    def _log_warn(msg: str) -> None:
        carb.log_warn(msg)
except ImportError:  # pragma: no cover
    _log = logging.getLogger(__name__)

    def _log_warn(msg: str) -> None:
        _log.warning(msg)


TRAJECTORY_EXT = ".json"
SCHEMA_VERSION = "1.0"


def build_trajectory_payload(
    name: str,
    environment: str,
    camera_path: str,
    frames: List[Dict[str, Any]],
    fps: int = 60,
    created: str = "",
) -> Dict[str, Any]:
    """Assemble a schema-conformant trajectory dict.

    *created* defaults to ``datetime.now().isoformat()`` when empty —
    callable-level control of the timestamp is useful when writing tests
    that need deterministic output.
    """
    return {
        "version": SCHEMA_VERSION,
        "name": name,
        "environment": environment,
        "camera_path": camera_path,
        "fps": fps,
        "frame_count": len(frames),
        "created": created or datetime.now().isoformat(),
        "frames": list(frames),
    }


def read_trajectory_json(filepath: str) -> Dict[str, Any]:
    """Load and return the trajectory dict at *filepath*.

    Raises the usual :class:`OSError` / :class:`json.JSONDecodeError`
    on missing or malformed files — the caller decides how to react.
    """
    with open(filepath, "r") as fh:
        return json.load(fh)


def write_trajectory_json(data: Dict[str, Any], filepath: str) -> str:
    """Write *data* to *filepath* as indented JSON.

    Creates parent directories on demand.  Returns the filepath.
    """
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as fh:
        json.dump(data, fh, indent=2)
    return filepath


def list_trajectory_files(directory: str) -> List[str]:
    """Return sorted absolute paths of trajectory JSONs in *directory*.

    Empty list if *directory* is empty, missing, or unreadable.
    """
    if not directory or not os.path.isdir(directory):
        return []
    try:
        return [
            os.path.join(directory, name)
            for name in sorted(os.listdir(directory))
            if name.endswith(TRAJECTORY_EXT)
        ]
    except OSError:
        return []


def list_trajectory_names(directory: str) -> List[str]:
    """Return sorted filenames (no path) of trajectory JSONs in *directory*."""
    if not directory or not os.path.isdir(directory):
        return []
    try:
        return sorted(
            n for n in os.listdir(directory) if n.endswith(TRAJECTORY_EXT)
        )
    except OSError:
        return []


def list_trajectory_info(directory: str) -> List[Dict[str, Any]]:
    """Return per-trajectory ``{name, path, frame_count}`` dicts.

    Malformed JSON files are reported with ``frame_count == -1``
    instead of raising, so the UI can still display them.
    """
    result: List[Dict[str, Any]] = []
    for filepath in list_trajectory_files(directory):
        try:
            data = read_trajectory_json(filepath)
            result.append({
                "name": os.path.basename(filepath),
                "path": filepath,
                "frame_count": data.get(
                    "frame_count", len(data.get("frames", []))
                ),
            })
        except Exception as exc:  # noqa: BLE001
            _log_warn(f"[BLV] Could not read trajectory {filepath}: {exc}")
            result.append({
                "name": os.path.basename(filepath),
                "path": filepath,
                "frame_count": -1,
            })
    return result
