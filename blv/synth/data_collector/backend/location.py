"""LocationManager — filesystem CRUD for named locations within an environment.

A *location* is a named spawn point inside an environment.  Each location
is a subdirectory of ``{root}/{class}/{environment}/`` that contains a
``location.json`` file storing the asset's spawn transform.

This module is pure Python: ``carb`` is imported opportunistically so
the module is usable from unit tests without Isaac Sim on the
PYTHONPATH.  The fallback logger mirrors the ``carb.log_*`` API so
behavior is identical aside from the backing sink.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Tuple

try:
    import carb  # type: ignore

    def _log_info(msg: str) -> None:
        carb.log_info(msg)

    def _log_warn(msg: str) -> None:
        carb.log_warn(msg)

    def _log_error(msg: str) -> None:
        carb.log_error(msg)
except ImportError:  # pragma: no cover
    _log = logging.getLogger(__name__)

    def _log_info(msg: str) -> None:
        _log.info(msg)

    def _log_warn(msg: str) -> None:
        _log.warning(msg)

    def _log_error(msg: str) -> None:
        _log.error(msg)


_LOCATION_FILE = "location.json"
_SCHEMA_VERSION = "1.0"
_SAFE_CHAR_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


class LocationManager:
    """Manages named locations on disk.

    Each location lives at ``{base_dir}/{name}/`` and contains a
    ``location.json`` with the asset spawn transform.
    """

    def __init__(self, base_directory: str = "") -> None:
        self._base_dir: str = base_directory
        self._current_location: str = ""

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def base_directory(self) -> str:
        return self._base_dir

    @property
    def current_location(self) -> str:
        return self._current_location

    @current_location.setter
    def current_location(self, name: str) -> None:
        self._current_location = name

    @property
    def has_location_selected(self) -> bool:
        return bool(self._current_location)

    # ------------------------------------------------------------------ #
    #  Directory helpers                                                   #
    # ------------------------------------------------------------------ #

    def set_base_directory(
        self, root_folder: str, class_name: str, environment: str
    ) -> str:
        """Derive and store the base directory from project settings."""
        expanded = os.path.normpath(os.path.expanduser(root_folder))
        self._base_dir = os.path.join(expanded, class_name, environment)
        return self._base_dir

    def get_location_directory(self, name: str) -> str:
        return os.path.join(self._base_dir, name)

    def get_trajectory_directory(self, name: str) -> str:
        return os.path.join(self._base_dir, name, "trajectories")

    # ------------------------------------------------------------------ #
    #  Validation                                                          #
    # ------------------------------------------------------------------ #

    def validate_name(self, name: str) -> Tuple[bool, str]:
        """Return ``(ok, error_message)`` for a candidate location name."""
        if not name or not name.strip():
            return False, "Location name cannot be empty."

        sanitized = _SAFE_CHAR_RE.sub("_", name.strip())
        if sanitized != name.strip():
            return False, (
                f"Name contains invalid characters.  Suggested: '{sanitized}'"
            )

        if self._base_dir and os.path.isdir(
            os.path.join(self._base_dir, name)
        ):
            return False, f"A location named '{name}' already exists."

        return True, ""

    # ------------------------------------------------------------------ #
    #  CRUD                                                                #
    # ------------------------------------------------------------------ #

    def list_locations(self) -> List[str]:
        """Return sorted names of locations that have a valid ``location.json``."""
        if not self._base_dir or not os.path.isdir(self._base_dir):
            return []
        locations: List[str] = []
        try:
            for entry in sorted(os.listdir(self._base_dir)):
                loc_dir = os.path.join(self._base_dir, entry)
                if os.path.isdir(loc_dir) and os.path.isfile(
                    os.path.join(loc_dir, _LOCATION_FILE)
                ):
                    locations.append(entry)
        except OSError as exc:
            _log_warn(f"[BLV] Failed to list locations: {exc}")
        return locations

    def create_location(
        self,
        name: str,
        translate: List[float],
        orient: List[float],
        scale: List[float],
    ) -> str:
        """Create a new location directory and write ``location.json``.

        Raises :class:`ValueError` if the name fails validation.
        """
        ok, err = self.validate_name(name)
        if not ok:
            raise ValueError(err)

        loc_dir = os.path.join(self._base_dir, name)
        os.makedirs(loc_dir, exist_ok=True)

        data = self._build_location_data(name, translate, orient, scale)
        filepath = os.path.join(loc_dir, _LOCATION_FILE)
        with open(filepath, "w") as fh:
            json.dump(data, fh, indent=2)

        _log_info(f"[BLV] Created location '{name}' → {filepath}")
        return filepath

    def load_location(self, name: str) -> Dict[str, Any]:
        """Read and return the parsed ``location.json`` for *name*."""
        filepath = os.path.join(self._base_dir, name, _LOCATION_FILE)
        with open(filepath, "r") as fh:
            return json.load(fh)

    def save_transform(
        self,
        name: str,
        translate: List[float],
        orient: List[float],
        scale: List[float],
    ) -> None:
        """Update *only* the ``spawn_transform`` in an existing ``location.json``.

        Other metadata (version, name, created, …) is preserved when
        present.  If the file is missing or corrupt the record is
        rebuilt from scratch.
        """
        filepath = os.path.join(self._base_dir, name, _LOCATION_FILE)
        try:
            with open(filepath, "r") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            data = self._build_location_data(name, translate, orient, scale)

        data["spawn_transform"] = {
            "translate": list(translate),
            "orient": list(orient),
            "scale": list(scale),
        }

        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w") as fh:
            json.dump(data, fh, indent=2)
        _log_info(f"[BLV] Saved transform for location '{name}'")

    def delete_location(self, name: str) -> bool:
        """Remove the location directory and all its contents."""
        loc_dir = os.path.join(self._base_dir, name)
        if not os.path.isdir(loc_dir):
            _log_warn(f"[BLV] Location directory not found: {loc_dir}")
            return False
        try:
            shutil.rmtree(loc_dir)
            _log_info(f"[BLV] Deleted location '{name}' at {loc_dir}")
            if self._current_location == name:
                self._current_location = ""
            return True
        except OSError as exc:
            _log_error(f"[BLV] Failed to delete location '{name}': {exc}")
            return False

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_location_data(
        name: str,
        translate: List[float],
        orient: List[float],
        scale: List[float],
    ) -> Dict[str, Any]:
        return {
            "version": _SCHEMA_VERSION,
            "name": name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "spawn_transform": {
                "translate": list(translate),
                "orient": list(orient),
                "scale": list(scale),
            },
        }
