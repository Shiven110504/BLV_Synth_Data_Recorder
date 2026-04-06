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

    # File extensions recognised as USD assets (searched recursively)
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
        """Display name of the currently loaded asset, or ``"None"``.

        When assets live in subdirectories (e.g. ``{name}/asset.usdz``) the
        parent directory name is used because the filename alone (``asset``)
        is not unique.
        """
        if 0 <= self._current_index < len(self._assets):
            return self._asset_display_name(self._assets[self._current_index])
        return "None"

    @property
    def current_asset_path(self) -> str:
        """Full path of the currently loaded asset, or empty string."""
        if 0 <= self._current_index < len(self._assets):
            return self._assets[self._current_index]
        return ""

    @property
    def current_asset_stem(self) -> str:
        """Unique stem identifier for the currently loaded asset.

        Uses the parent directory name when the filename is generic
        (e.g. ``asset.usdz``).
        """
        if 0 <= self._current_index < len(self._assets):
            return self._asset_display_name(self._assets[self._current_index])
        return ""

    def _asset_display_name(self, asset_path: str) -> str:
        """Return a meaningful display name for *asset_path*.

        If the file sits directly in the scanned folder, use its stem.
        Otherwise use the immediate parent directory name (which is typically
        the unique identifier when every file is named ``asset.usdz``).
        """
        parent = os.path.dirname(asset_path)
        if os.path.normpath(parent) == os.path.normpath(self._asset_folder):
            # File is directly in the scanned folder — use its stem
            return os.path.splitext(os.path.basename(asset_path))[0]
        # File is in a subdirectory — use the directory name
        return os.path.basename(parent)

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

        carb.log_warn(f"[BLV][DEBUG] set_folder: folder_path='{folder_path}'")
        carb.log_warn(f"[BLV][DEBUG] set_folder: isdir={os.path.isdir(folder_path)}")

        # Collect and sort all matching files — search recursively so that
        # assets inside subdirectories (e.g. {asset_name}/asset.usdz) are found.
        self._assets = sorted(
            path
            for ext in self.USD_EXTENSIONS
            for path in _glob.glob(os.path.join(folder_path, "**", ext), recursive=True)
        )
        self._current_index = -1

        # Debug: show what glob patterns matched
        for ext in self.USD_EXTENSIONS:
            flat_pattern = os.path.join(folder_path, ext)
            recursive_pattern = os.path.join(folder_path, "**", ext)
            flat_matches = _glob.glob(flat_pattern)
            recursive_matches = _glob.glob(recursive_pattern, recursive=True)
            carb.log_warn(
                f"[BLV][DEBUG] glob flat('{flat_pattern}') → {len(flat_matches)}, "
                f"recursive('{recursive_pattern}') → {len(recursive_matches)} matches"
            )
            if recursive_matches:
                for m in recursive_matches[:3]:
                    carb.log_warn(f"[BLV][DEBUG]   sample: {m}")
                if len(recursive_matches) > 3:
                    carb.log_warn(f"[BLV][DEBUG]   ... and {len(recursive_matches) - 3} more")

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
        carb.log_warn(f"[BLV][DEBUG] load_asset: index={index}")
        carb.log_warn(f"[BLV][DEBUG] load_asset: asset_path='{asset_path}'")
        carb.log_warn(f"[BLV][DEBUG] load_asset: file_exists={os.path.isfile(asset_path)}")
        carb.log_warn(f"[BLV][DEBUG] load_asset: target_prim_path='{self._target_prim_path}'")
        carb.log_warn(f"[BLV][DEBUG] load_asset: class_name='{self._class_name}'")

        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available.")
            return False

        # Ensure target prim path is a valid absolute USD path
        if not self._target_prim_path or not self._target_prim_path.startswith("/"):
            old_path = self._target_prim_path
            self._target_prim_path = "/World/" + (self._target_prim_path or "TargetAsset")
            carb.log_warn(
                f"[BLV][DEBUG] Target prim path was not absolute: '{old_path}' "
                f"→ auto-corrected to '{self._target_prim_path}'"
            )

        # Ensure the target prim exists (create an Xform if not)
        prim = stage.GetPrimAtPath(self._target_prim_path)
        if not prim.IsValid():
            carb.log_warn(
                f"[BLV][DEBUG] Prim not found at '{self._target_prim_path}', creating Xform..."
            )
            prim = stage.DefinePrim(self._target_prim_path, "Xform")
            carb.log_info(f"[BLV] Created target prim at {self._target_prim_path}")

        try:
            # Clear all existing references on the target prim, then add the
            # new one.  This cleanly swaps to a different asset file.
            refs = prim.GetReferences()
            refs.ClearReferences()
            carb.log_warn(f"[BLV][DEBUG] Adding reference: '{asset_path}'")
            refs.AddReference(asset_path)
            carb.log_warn(f"[BLV][DEBUG] Reference added successfully")
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
