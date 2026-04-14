"""
LocationManager — filesystem CRUD for named locations within an environment.
=============================================================================

A *location* is a named spawn point inside an environment.  Each location is
a subdirectory of ``{root}/{class}/{environment}/`` that contains a
``location.json`` file storing the asset's spawn transform.

This module is a pure filesystem utility — it has **no** USD or ``omni``
imports so it can be tested and reasoned about independently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import carb

_LOCATION_FILE = "location.json"
_SCHEMA_VERSION = "1.0"
# Characters allowed in a location folder name.
_SAFE_CHAR_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


class LocationManager:
    """Manages named locations on disk.

    Each location lives at ``{base_dir}/{name}/`` and contains a
    ``location.json`` with the asset spawn transform.

    Parameters
    ----------
    base_directory : str, optional
        The ``{root}/{class}/{environment}`` directory.  Can be set later
        via :meth:`set_base_directory`.
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
        """Derive and store the base directory from project settings.

        Returns the resolved path.
        """
        expanded = os.path.normpath(os.path.expanduser(root_folder))
        self._base_dir = os.path.join(expanded, class_name, environment)
        return self._base_dir

    def get_location_directory(self, name: str) -> str:
        """Return ``{base}/{name}/``."""
        return os.path.join(self._base_dir, name)

    def get_trajectory_directory(self, name: str) -> str:
        """Return ``{base}/{name}/trajectories/``."""
        return os.path.join(self._base_dir, name, "trajectories")

    # ------------------------------------------------------------------ #
    #  Validation                                                          #
    # ------------------------------------------------------------------ #

    def validate_name(self, name: str) -> Tuple[bool, str]:
        """Check whether *name* is a valid, non-duplicate location name.

        Returns ``(ok, error_message)``.
        """
        if not name or not name.strip():
            return False, "Location name cannot be empty."

        sanitized = _SAFE_CHAR_RE.sub("_", name.strip())
        if sanitized != name.strip():
            return False, (
                f"Name contains invalid characters.  "
                f"Suggested: '{sanitized}'"
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
            carb.log_warn(f"[BLV] Failed to list locations: {exc}")
        return locations

    def create_location(
        self,
        name: str,
        translate: List[float],
        orient: List[float],
        scale: List[float],
    ) -> str:
        """Create a new location directory and write ``location.json``.

        Parameters
        ----------
        name : str
            Location name (becomes the folder name).
        translate : list[float]
            ``[x, y, z]`` position.
        orient : list[float]
            ``[w, x, y, z]`` quaternion.
        scale : list[float]
            ``[x, y, z]`` scale factors.

        Returns
        -------
        str
            Absolute path to the created ``location.json``.

        Raises
        ------
        ValueError
            If the name fails validation.
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

        carb.log_info(f"[BLV] Created location '{name}' → {filepath}")
        return filepath

    def load_location(self, name: str) -> Dict[str, Any]:
        """Read and return the parsed ``location.json`` for *name*.

        Raises ``FileNotFoundError`` or ``json.JSONDecodeError`` on failure.
        """
        filepath = os.path.join(self._base_dir, name, _LOCATION_FILE)
        with open(filepath, "r") as fh:
            data = json.load(fh)
        return data

    def save_transform(
        self,
        name: str,
        translate: List[float],
        orient: List[float],
        scale: List[float],
    ) -> None:
        """Update *only* the ``spawn_transform`` in an existing ``location.json``.

        Other metadata fields (version, name, created, …) are preserved.
        """
        filepath = os.path.join(self._base_dir, name, _LOCATION_FILE)
        try:
            with open(filepath, "r") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            # File missing or corrupt — rebuild from scratch.
            data = self._build_location_data(name, translate, orient, scale)

        data["spawn_transform"] = {
            "translate": list(translate),
            "orient": list(orient),
            "scale": list(scale),
        }

        with open(filepath, "w") as fh:
            json.dump(data, fh, indent=2)
        carb.log_info(f"[BLV] Saved transform for location '{name}'")

    def delete_location(self, name: str) -> bool:
        """Remove the location directory and all its contents.

        Returns ``True`` on success.
        """
        loc_dir = os.path.join(self._base_dir, name)
        if not os.path.isdir(loc_dir):
            carb.log_warn(f"[BLV] Location directory not found: {loc_dir}")
            return False
        try:
            shutil.rmtree(loc_dir)
            carb.log_info(f"[BLV] Deleted location '{name}' at {loc_dir}")
            if self._current_location == name:
                self._current_location = ""
            return True
        except OSError as exc:
            carb.log_error(f"[BLV] Failed to delete location '{name}': {exc}")
            return False

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
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
