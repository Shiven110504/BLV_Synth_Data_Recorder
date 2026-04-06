"""
AssetBrowser — USD asset folder browser with semantic labeling.
===============================================================

Scans a local directory for USD files (``*.usd``, ``*.usda``, ``*.usdc``,
``*.usdz``), presents them as a navigable list, and loads each one as a **USD
reference** on a configurable target prim.

Semantic labeling
-----------------
Every time an asset is loaded the browser:

1. **Removes** the configured class label from any prims that previously had it
   (tracked via an internal cache — no expensive full-stage traversal on every
   swap).
2. **Adds** the class label to the target prim so that Replicator annotators
   (semantic segmentation, bounding boxes) pick it up correctly.

This uses Isaac Sim's ``isaacsim.core.utils.semantics.add_labels`` /
``remove_labels`` helpers which write the ``Semantics`` schema to USD prims.
"""

from __future__ import annotations

import glob as _glob
import os
from typing import List, Optional, Set

import carb
import omni.usd
from pxr import Sdf, Usd, UsdGeom


class AssetBrowser:
    """Browse and swap USD assets from a local folder.

    Parameters
    ----------
    asset_folder : str
        Initial directory to scan (can be changed later via :meth:`set_folder`).
    class_name : str
        Semantic class label applied to each loaded asset (e.g.
        ``"elevator_button"``).
    target_prim_path : str
        USD prim path where asset references will be loaded.
    """

    # File extensions recognised as USD assets
    USD_EXTENSIONS: tuple = ("*.usd", "*.usda", "*.usdc", "*.usdz")

    def __init__(
        self,
        asset_folder: str = "",
        class_name: str = "",
        target_prim_path: str = "/World/TargetAsset",
    ) -> None:
        self._asset_folder: str = asset_folder
        self._class_name: str = class_name
        self._target_prim_path: str = target_prim_path

        self._assets: List[str] = []
        self._current_index: int = -1

        # Cache of prim paths that currently carry our class label so we can
        # remove it without traversing the entire stage every time.
        self._labeled_prim_paths: Set[str] = set()

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def current_index(self) -> int:
        """Zero-based index of the currently loaded asset (``-1`` if none)."""
        return self._current_index

    @property
    def total_assets(self) -> int:
        """Number of USD files found in the active folder."""
        return len(self._assets)

    @property
    def current_asset_name(self) -> str:
        """Basename of the currently loaded asset, or ``"None"``."""
        if 0 <= self._current_index < len(self._assets):
            return os.path.basename(self._assets[self._current_index])
        return "None"

    @property
    def current_asset_path(self) -> str:
        """Full path of the currently loaded asset, or empty string."""
        if 0 <= self._current_index < len(self._assets):
            return self._assets[self._current_index]
        return ""

    @property
    def current_asset_stem(self) -> str:
        """Filename without extension of the currently loaded asset."""
        if 0 <= self._current_index < len(self._assets):
            return os.path.splitext(os.path.basename(self._assets[self._current_index]))[0]
        return ""

    @property
    def class_name(self) -> str:
        return self._class_name

    @class_name.setter
    def class_name(self, name: str) -> None:
        self._class_name = name

    @property
    def target_prim_path(self) -> str:
        return self._target_prim_path

    @target_prim_path.setter
    def target_prim_path(self, path: str) -> None:
        self._target_prim_path = path

    @property
    def asset_folder(self) -> str:
        return self._asset_folder

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def set_folder(self, folder_path: str, class_name: Optional[str] = None) -> int:
        """Scan *folder_path* for USD files and optionally update the class name.

        Parameters
        ----------
        folder_path : str
            Directory containing USD asset files.
        class_name : str, optional
            If provided, updates the semantic class label.

        Returns
        -------
        int
            Number of USD files found.
        """
        folder_path = os.path.normpath(os.path.expanduser(folder_path))
        self._asset_folder = folder_path
        if class_name is not None:
            self._class_name = class_name

        # Collect and sort all matching files
        self._assets = sorted(
            path
            for ext in self.USD_EXTENSIONS
            for path in _glob.glob(os.path.join(folder_path, ext))
        )
        self._current_index = -1
        carb.log_info(
            f"[BLV] AssetBrowser scanned '{folder_path}' — "
            f"{len(self._assets)} USD files found."
        )
        return len(self._assets)

    def set_target_prim(self, prim_path: str) -> None:
        """Set the USD prim path where assets are loaded as references."""
        if prim_path and not prim_path.startswith("/"):
            prim_path = "/World/" + prim_path
            carb.log_warn(
                f"[BLV] Target prim path was not absolute — auto-corrected to '{prim_path}'"
            )
        self._target_prim_path = prim_path

    def next_asset(self) -> bool:
        """Load the next asset in the sorted list (wraps around).

        Returns
        -------
        bool
            ``True`` on success.
        """
        if not self._assets:
            carb.log_warn("[BLV] No assets available — set a folder first.")
            return False
        next_idx = (self._current_index + 1) % len(self._assets)
        return self.load_asset(next_idx)

    def previous_asset(self) -> bool:
        """Load the previous asset in the sorted list (wraps around).

        Returns
        -------
        bool
            ``True`` on success.
        """
        if not self._assets:
            carb.log_warn("[BLV] No assets available — set a folder first.")
            return False
        prev_idx = (self._current_index - 1) % len(self._assets)
        return self.load_asset(prev_idx)

    def load_asset(self, index: int) -> bool:
        """Load a specific asset by its index in the sorted file list.

        The asset is applied as a USD **reference** on the target prim.  Any
        previous reference on that prim is cleared first.  After loading, the
        semantic class label is applied (and removed from any prims that had it
        before).

        Parameters
        ----------
        index : int
            Zero-based index into the sorted asset list.

        Returns
        -------
        bool
            ``True`` if the asset was loaded successfully.
        """
        if index < 0 or index >= len(self._assets):
            carb.log_error(f"[BLV] Asset index {index} out of range [0, {len(self._assets)}).")
            return False

        asset_path = self._assets[index]
        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available.")
            return False

        # Ensure target prim path is a valid absolute USD path
        if not self._target_prim_path or not self._target_prim_path.startswith("/"):
            self._target_prim_path = "/World/" + (self._target_prim_path or "TargetAsset")
            carb.log_warn(
                f"[BLV] Target prim path was not absolute — auto-corrected to "
                f"'{self._target_prim_path}'"
            )

        # Ensure the target prim exists (create an Xform if not)
        prim = stage.GetPrimAtPath(self._target_prim_path)
        if not prim.IsValid():
            prim = stage.DefinePrim(self._target_prim_path, "Xform")
            carb.log_info(f"[BLV] Created target prim at {self._target_prim_path}")

        try:
            # Clear all existing references on the target prim, then add the
            # new one.  This cleanly swaps to a different asset file.
            refs = prim.GetReferences()
            refs.ClearReferences()
            refs.AddReference(asset_path)
        except Exception as exc:
            carb.log_error(f"[BLV] Failed to set reference on {self._target_prim_path}: {exc}")
            return False

        self._current_index = index

        # Update semantic labels
        self._apply_semantic_label(stage, prim)

        carb.log_info(
            f"[BLV] Loaded asset [{index + 1}/{len(self._assets)}] "
            f"'{os.path.basename(asset_path)}' → {self._target_prim_path}"
        )
        return True

    # ------------------------------------------------------------------ #
    #  Internal — Semantic labeling                                       #
    # ------------------------------------------------------------------ #

    def _apply_semantic_label(self, stage: Usd.Stage, target_prim: Usd.Prim) -> None:
        """Apply the class label to *target_prim* and remove it from any prim
        that previously had it.

        We maintain ``_labeled_prim_paths`` as a cache so that we only touch
        prims we know about, rather than traversing the entire stage every
        time an asset is swapped.
        """
        # Lazy import — Isaac Sim semantics helpers
        try:
            from isaacsim.core.utils.semantics import add_labels, remove_labels
        except ImportError:
            carb.log_error(
                "[BLV] Could not import isaacsim.core.utils.semantics — "
                "semantic labeling unavailable."
            )
            return

        # 1) Remove the class label from previously labeled prims
        stale_paths = set(self._labeled_prim_paths)  # copy
        for path in stale_paths:
            if path == str(target_prim.GetPath()):
                # Will be re-applied below; skip removal for efficiency
                continue
            old_prim = stage.GetPrimAtPath(path)
            if old_prim.IsValid():
                try:
                    remove_labels(old_prim, instance_name="class")
                except Exception as exc:
                    carb.log_warn(
                        f"[BLV] Failed to remove label from {path}: {exc}"
                    )
        self._labeled_prim_paths.clear()

        # 2) Add the class label to the target prim
        if self._class_name:
            try:
                add_labels(target_prim, labels=[self._class_name], instance_name="class")
                self._labeled_prim_paths.add(str(target_prim.GetPath()))
                carb.log_info(
                    f"[BLV] Semantic label '{self._class_name}' applied to "
                    f"{target_prim.GetPath()}"
                )
            except Exception as exc:
                carb.log_error(
                    f"[BLV] Failed to add semantic label to "
                    f"{target_prim.GetPath()}: {exc}"
                )
