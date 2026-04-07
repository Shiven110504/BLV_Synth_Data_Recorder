"""
AssetBrowser — swap USD assets on a target prim from a local folder.
====================================================================

Scans a directory for USD files and lets the user step through them
with next/prev.  Each swap edits the **reference list directly on the
Sdf prim spec** in whichever layer authored the target prim.  This is
the only approach that reliably works because:

  - The target prim (e.g. /World/TargetAsset) is authored in the root
    scene layer (e.g. HotelCorridor.usd) with a relative reference
    path.  Kit commands like DeletePrims + CreateReference may write to
    the wrong layer or the session layer.
  - MetricsAssembler's unitsResolve xformOps live on a separate
    auto-generated layer and must not be disturbed.
  - Sdf-level edits trigger a proper recomposition — Hydra redraws,
    MetricsAssembler re-evaluates.

The swap approach (proven by the diagnostic script):
  1. Find the layer that has a prim spec for the target prim.
  2. Clear all reference edits on that spec.
  3. Author a new explicit reference to the new asset file.
  → The composed prim picks up the new mesh, MetricsAssembler applies
    unitsResolve, and all existing xformOps (translate, orient, scale)
    stay because they were authored on the same spec.
"""

from __future__ import annotations

import glob as _glob
import os
from typing import List, Optional, Set

import carb
import omni.usd
from pxr import Gf, Sdf, Usd, UsdGeom


class AssetBrowser:
    """Browse and swap USD assets from a local folder."""

    USD_EXTENSIONS: tuple = ("*.usd", "*.usda", "*.usdc", "*.usdz")

    def __init__(
        self,
        asset_folder: str = "",
        class_name: str = "",
        target_prim_path: str = "/World/TargetAsset",
    ) -> None:
        self._asset_folder = asset_folder
        self._class_name = class_name
        self._target_prim_path = target_prim_path

        self._assets: List[str] = []
        self._current_index: int = -1
        self._labeled_prim_paths: Set[str] = set()

    # ── properties ─────────────────────────────────────────────────────

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def total_assets(self) -> int:
        return len(self._assets)

    @property
    def current_asset_name(self) -> str:
        if 0 <= self._current_index < len(self._assets):
            return os.path.basename(self._assets[self._current_index])
        return "None"

    @property
    def current_asset_path(self) -> str:
        if 0 <= self._current_index < len(self._assets):
            return self._assets[self._current_index]
        return ""

    @property
    def current_asset_stem(self) -> str:
        if 0 <= self._current_index < len(self._assets):
            return os.path.splitext(os.path.basename(
                self._assets[self._current_index]))[0]
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

    # ── public API ─────────────────────────────────────────────────────

    def set_folder(self, folder_path: str, class_name: Optional[str] = None) -> int:
        """Scan folder for USD files. Returns count found."""
        folder_path = os.path.normpath(os.path.expanduser(folder_path))
        self._asset_folder = folder_path
        if class_name is not None:
            self._class_name = class_name

        self._assets = sorted(
            p for ext in self.USD_EXTENSIONS
            for p in _glob.glob(os.path.join(folder_path, ext))
        )
        self._current_index = -1
        carb.log_info(
            f"[BLV] AssetBrowser: {len(self._assets)} USD files in '{folder_path}'"
        )
        return len(self._assets)

    def set_target_prim(self, prim_path: str) -> None:
        if prim_path and not prim_path.startswith("/"):
            prim_path = "/World/" + prim_path
        self._target_prim_path = prim_path

    def next_asset(self) -> bool:
        if not self._assets:
            carb.log_warn("[BLV] No assets — scan a folder first.")
            return False
        return self.load_asset((self._current_index + 1) % len(self._assets))

    def previous_asset(self) -> bool:
        if not self._assets:
            carb.log_warn("[BLV] No assets — scan a folder first.")
            return False
        return self.load_asset((self._current_index - 1) % len(self._assets))

    def load_asset(self, index: int) -> bool:
        """Load asset at index by swapping the reference on the target prim.

        This operates at the Sdf layer level:
          1. Find the layer that has a prim spec for the target.
          2. Clear all reference edits on that spec.
          3. Author a new explicit reference to the asset file.

        Everything else (transform, MetricsAssembler unitsResolve, etc.)
        is untouched because we only modify the referenceList.
        """
        if index < 0 or index >= len(self._assets):
            carb.log_error(f"[BLV] Index {index} out of range.")
            return False

        asset_path = os.path.abspath(self._assets[index])
        if not os.path.isfile(asset_path):
            carb.log_error(f"[BLV] File not found: {asset_path}")
            return False

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage.")
            return False

        target = self._target_prim_path
        sdf_path = Sdf.Path(target)

        # ── Find the layer that owns this prim's reference ─────────────
        # Walk the layer stack (strongest first) and find the layer
        # that has a prim spec for our target path.
        authoring_layer = None
        for layer in stage.GetLayerStack(includeSessionLayers=True):
            if layer.GetPrimAtPath(sdf_path) is not None:
                authoring_layer = layer
                break

        if authoring_layer is None:
            # Target prim doesn't exist yet — create it in the root layer
            authoring_layer = stage.GetRootLayer()
            carb.log_info(
                f"[BLV] No existing spec for {target} — "
                f"will create in root layer."
            )

        # ── Swap the reference at the Sdf layer level ──────────────────
        try:
            prim_spec = authoring_layer.GetPrimAtPath(sdf_path)
            if prim_spec is None:
                # Create the prim spec
                prim_spec = Sdf.CreatePrimInLayer(authoring_layer, sdf_path)
                prim_spec.specifier = Sdf.SpecifierDef
                prim_spec.typeName = "Xform"

            # Clear ALL reference edits (explicit, prepended, appended, etc.)
            prim_spec.referenceList.ClearEdits()

            # Author a single explicit reference to the new asset
            prim_spec.referenceList.explicitItems = [
                Sdf.Reference(assetPath=asset_path)
            ]

        except Exception as exc:
            carb.log_error(f"[BLV] Sdf reference swap failed: {exc}")
            return False

        # ── Verify the composed prim picked up the new reference ───────
        prim = stage.GetPrimAtPath(target)
        if not prim.IsValid():
            carb.log_error(f"[BLV] Prim {target} invalid after swap.")
            return False

        # ── Apply semantic label ───────────────────────────────────────
        self._apply_semantic_label(stage, prim)

        self._current_index = index
        carb.log_warn(
            f"[BLV] Loaded asset [{index + 1}/{len(self._assets)}] "
            f"'{os.path.basename(asset_path)}' → {target}"
        )
        return True

    # ── semantic labeling ──────────────────────────────────────────────

    def _apply_semantic_label(self, stage: Usd.Stage, target_prim: Usd.Prim) -> None:
        """Apply class label to target_prim, remove from previously labeled prims."""
        try:
            from isaacsim.core.utils.semantics import add_labels, remove_labels
        except ImportError:
            carb.log_error("[BLV] Cannot import isaacsim.core.utils.semantics.")
            return

        for path in set(self._labeled_prim_paths):
            if path == str(target_prim.GetPath()):
                continue
            old = stage.GetPrimAtPath(path)
            if old.IsValid():
                try:
                    remove_labels(old, instance_name="class")
                except Exception:
                    pass
        self._labeled_prim_paths.clear()

        if self._class_name:
            try:
                add_labels(target_prim, labels=[self._class_name],
                           instance_name="class")
                self._labeled_prim_paths.add(str(target_prim.GetPath()))
            except Exception as exc:
                carb.log_error(f"[BLV] Semantic label failed: {exc}")
