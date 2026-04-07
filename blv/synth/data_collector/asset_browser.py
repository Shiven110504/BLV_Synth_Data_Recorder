"""
AssetBrowser — swap USD assets on a target prim from a local folder.
====================================================================

Scans a directory for USD files and lets the user step through them with
next/prev.  Each asset is loaded via the Kit ``CreateReference`` command
(which handles prim creation + reference in one atomic step) after first
deleting the old target prim with ``DeletePrims``.

The user manually positions the target prim once.  On every swap the
extension reads the current transform, deletes the prim, re-creates it
with the new reference, then writes the saved transform back.

Semantic labeling is applied via ``isaacsim.core.utils.semantics.add_labels``
so that Replicator annotators pick up the object class.
"""

from __future__ import annotations

import glob as _glob
import os
from pathlib import Path
from typing import List, Optional, Set, Tuple

import carb
import omni.kit.commands
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
        self._asset_folder: str = asset_folder
        self._class_name: str = class_name
        self._target_prim_path: str = target_prim_path

        self._assets: List[str] = []
        self._current_index: int = -1

        # Cache of prim paths that carry our semantic label
        self._labeled_prim_paths: Set[str] = set()

        # Saved transform — set once by the user, reused on every swap
        self._saved_translate: Gf.Vec3d = Gf.Vec3d(0, 0, 0)
        self._saved_rotate: Gf.Vec3f = Gf.Vec3f(0, 0, 0)
        self._saved_scale: Gf.Vec3f = Gf.Vec3f(1, 1, 1)
        self._has_saved_xform: bool = False

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

    # ── public API ─────────────────────────────────────────────────────

    def set_folder(self, folder_path: str, class_name: Optional[str] = None) -> int:
        """Scan folder_path for USD files.  Returns the count found."""
        folder_path = os.path.normpath(os.path.expanduser(folder_path))
        self._asset_folder = folder_path
        if class_name is not None:
            self._class_name = class_name

        self._assets = sorted(
            p for ext in self.USD_EXTENSIONS
            for p in _glob.glob(os.path.join(folder_path, ext))
        )
        self._current_index = -1
        self._has_saved_xform = False

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
        """Load asset at index onto the target prim.

        Strategy:
          1. Read the target prim's current transform (if it exists).
          2. Clear the viewport selection (avoids Kit errors on child deletion).
          3. Delete the target prim entirely (removes all stale children/refs).
          4. Use Kit's CreateReference command to create an Xform + add reference.
          5. Write back the saved transform.
          6. Apply semantic label.
        """
        if index < 0 or index >= len(self._assets):
            carb.log_error(f"[BLV] Index {index} out of range [0, {len(self._assets)}).")
            return False

        asset_path = self._assets[index]

        # Validate the file actually exists
        if not os.path.isfile(asset_path):
            carb.log_error(f"[BLV] Asset file not found: {asset_path}")
            return False

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage.")
            return False

        target = self._target_prim_path

        # ── 1. Save the current transform ──────────────────────────────
        existing = stage.GetPrimAtPath(target)
        if existing.IsValid():
            t, r, s = self._read_xform(existing)
            self._saved_translate = t
            self._saved_rotate = r
            self._saved_scale = s
            self._has_saved_xform = True

        # ── 2. Clear selection ─────────────────────────────────────────
        try:
            omni.usd.get_context().get_selection().set_selected_prim_paths([], False)
        except Exception:
            pass

        # ── 3. Delete old prim ─────────────────────────────────────────
        if existing.IsValid():
            omni.kit.commands.execute(
                "DeletePrims",
                paths=[target],
                destructive=True,
            )

        # ── 4. Create new prim with reference ──────────────────────────
        # Use absolute file:/ URI for Omniverse path resolution
        abs_path = os.path.abspath(asset_path)
        asset_uri = abs_path  # Kit handles OS paths correctly

        try:
            omni.kit.commands.execute(
                "CreateReference",
                usd_context=omni.usd.get_context(),
                path_to=Sdf.Path(target),
                asset_path=asset_uri,
                prim_path=Sdf.Path.emptyPath,  # use defaultPrim from the file
            )
        except Exception as exc:
            carb.log_error(f"[BLV] CreateReference failed: {exc}")
            # Fallback: raw USD API
            try:
                prim = stage.DefinePrim(target, "Xform")
                prim.GetReferences().AddReference(asset_uri)
                carb.log_info("[BLV] Fallback: raw USD AddReference succeeded.")
            except Exception as exc2:
                carb.log_error(f"[BLV] Fallback also failed: {exc2}")
                return False

        # ── 5. Write transform ─────────────────────────────────────────
        prim = stage.GetPrimAtPath(target)
        if prim.IsValid() and self._has_saved_xform:
            self._write_xform(prim, self._saved_translate,
                              self._saved_rotate, self._saved_scale)

        # ── 6. Semantic label ──────────────────────────────────────────
        if prim.IsValid():
            self._apply_semantic_label(stage, prim)

        self._current_index = index
        carb.log_warn(
            f"[BLV] Loaded asset [{index + 1}/{len(self._assets)}] "
            f"'{os.path.basename(asset_path)}' → {target}"
        )
        return True

    # ── transform helpers ──────────────────────────────────────────────

    @staticmethod
    def _read_xform(prim: Usd.Prim) -> Tuple[Gf.Vec3d, Gf.Vec3f, Gf.Vec3f]:
        """Read translate/rotate/scale from prim, returning identity defaults
        for any missing op."""
        translate = Gf.Vec3d(0, 0, 0)
        rotate = Gf.Vec3f(0, 0, 0)
        scale = Gf.Vec3f(1, 1, 1)

        xf = UsdGeom.Xformable(prim)
        if not xf:
            return translate, rotate, scale

        for op in xf.GetOrderedXformOps():
            name = op.GetOpName()
            val = op.Get()
            if val is None:
                continue
            if "translate" in name:
                translate = Gf.Vec3d(val)
            elif "rotate" in name:
                rotate = Gf.Vec3f(val)
            elif "scale" in name:
                scale = Gf.Vec3f(val)
        return translate, rotate, scale

    @staticmethod
    def _write_xform(prim: Usd.Prim, translate: Gf.Vec3d,
                     rotate: Gf.Vec3f, scale: Gf.Vec3f) -> None:
        """Write translate/rotate/scale onto prim, handling precision
        mismatches from the referenced asset's existing attributes."""
        try:
            xf = UsdGeom.Xformable(prim)
            if not xf:
                return

            # Clear any xformOps the referenced asset brought in
            xf.ClearXformOpOrder()

            # Use XformCommonAPI for clean, standard ops.
            # This is the simplest approach — if it fails (because
            # the referenced asset defined incompatible ops), we fall
            # back to raw xformOps.
            common = UsdGeom.XformCommonAPI(prim)
            if common:
                common.SetTranslate(translate)
                common.SetRotate(rotate)
                common.SetScale(scale)
                return

            # Fallback: raw ops with precision detection
            def _get_prec(attr_name: str, default):
                if not prim.HasAttribute(attr_name):
                    return default
                tn = str(prim.GetAttribute(attr_name).GetTypeName())
                if "double" in tn:
                    return UsdGeom.XformOp.PrecisionDouble
                return UsdGeom.XformOp.PrecisionFloat

            t_prec = _get_prec("xformOp:translate", UsdGeom.XformOp.PrecisionDouble)
            t_op = xf.AddTranslateOp(t_prec)
            t_op.Set(Gf.Vec3d(translate) if t_prec == UsdGeom.XformOp.PrecisionDouble
                     else Gf.Vec3f(translate))

            r_prec = _get_prec("xformOp:rotateXYZ", UsdGeom.XformOp.PrecisionFloat)
            r_op = xf.AddRotateXYZOp(r_prec)
            r_op.Set(Gf.Vec3f(rotate))

            s_prec = _get_prec("xformOp:scale", UsdGeom.XformOp.PrecisionFloat)
            s_op = xf.AddScaleOp(s_prec)
            s_op.Set(Gf.Vec3f(scale))

            xf.SetXformOpOrder([t_op, r_op, s_op])
        except Exception as exc:
            carb.log_warn(f"[BLV] Transform write failed on {prim.GetPath()}: {exc}")

    # ── semantic labeling ──────────────────────────────────────────────

    def _apply_semantic_label(self, stage: Usd.Stage, target_prim: Usd.Prim) -> None:
        """Apply class label to target_prim and remove from previously labeled prims."""
        try:
            from isaacsim.core.utils.semantics import add_labels, remove_labels
        except ImportError:
            carb.log_error("[BLV] Cannot import isaacsim.core.utils.semantics.")
            return

        # Remove from old prims
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

        # Add to target
        if self._class_name:
            try:
                add_labels(target_prim, labels=[self._class_name], instance_name="class")
                self._labeled_prim_paths.add(str(target_prim.GetPath()))
                carb.log_info(f"[BLV] Label '{self._class_name}' → {target_prim.GetPath()}")
            except Exception as exc:
                carb.log_error(f"[BLV] Semantic label failed: {exc}")
