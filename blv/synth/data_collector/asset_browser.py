"""
AssetBrowser — swap USD assets on a target prim from a local folder.
====================================================================

Scans a directory for USD files and lets the user step through them with
next/prev.  Each swap:

  1. Reads the target prim's world-space translation (so the next asset
     appears in the same spot).
  2. Clears the viewport selection (avoids Kit manipulator crash on
     stale child paths).
  3. Deletes the old target prim entirely via ``DeletePrims`` command.
  4. Re-creates it with a reference via ``CreateReference`` command.
  5. Lets MetricsAssembler handle any metersPerUnit / upAxis mismatch
     (the SAM3d assets are in meters + Z-up, Isaac Sim is cm + Y-up).
  6. Overwrites ONLY the translate op so the asset lands at the saved
     position — scale and rotation corrections from MetricsAssembler
     are preserved.
  7. Applies the semantic class label.

Key insight: MetricsAssembler inserts ``xformOp:scale:unitsResolve``
and ``xformOp:rotateX:unitsResolve`` to fix the 100x scale and Z→Y
axis flip.  If we call ``ClearXformOpOrder()`` we destroy those fixes.
So we ONLY touch ``xformOp:translate`` and leave everything else alone.
"""

from __future__ import annotations

import glob as _glob
import os
from typing import List, Optional, Set

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
        self._labeled_prim_paths: Set[str] = set()

        # Saved position — set by the user once, reused on every swap.
        # We only save translate because scale/rotation are handled by
        # MetricsAssembler for unit correction.
        self._saved_translate: Optional[Gf.Vec3d] = None

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
        self._saved_translate = None   # reset on new folder

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
          1. Save current translate (if prim exists and user has positioned it).
          2. Clear viewport selection (avoids Kit manipulator crash).
          3. Delete the old target prim via Kit command.
          4. Create new prim + reference via Kit command.
          5. MetricsAssembler auto-fixes scale/rotation for unit mismatch.
          6. Overwrite ONLY the translate to restore user's position.
          7. Apply semantic label.
        """
        if index < 0 or index >= len(self._assets):
            carb.log_error(f"[BLV] Index {index} out of range.")
            return False

        asset_path = self._assets[index]
        if not os.path.isfile(asset_path):
            carb.log_error(f"[BLV] File not found: {asset_path}")
            return False

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage.")
            return False

        target = self._target_prim_path

        # ── 1. Save translate from current prim ────────────────────────
        existing = stage.GetPrimAtPath(target)
        if existing.IsValid():
            self._saved_translate = self._read_translate(existing)

        # ── 2. Clear viewport selection ────────────────────────────────
        # The old prim's children will be destroyed. If any are selected,
        # Kit's manipulator selector crashes with Ill-formed SdfPath.
        try:
            omni.usd.get_context().get_selection().set_selected_prim_paths([], False)
        except Exception:
            pass

        # ── 3. Delete old prim ─────────────────────────────────────────
        if existing.IsValid():
            try:
                omni.kit.commands.execute("DeletePrims",
                    paths=[target], destructive=True)
            except Exception:
                # Fallback: raw USD
                try:
                    stage.RemovePrim(target)
                except Exception as exc:
                    carb.log_error(f"[BLV] Cannot remove {target}: {exc}")

        # ── 4. Create new prim with reference ──────────────────────────
        abs_path = os.path.abspath(asset_path)

        try:
            omni.kit.commands.execute("CreateReference",
                usd_context=omni.usd.get_context(),
                path_to=Sdf.Path(target),
                asset_path=abs_path,
                prim_path=None,         # use file's defaultPrim
                instanceable=False,     # we need to write xformOps
                select_prim=False,      # avoid manipulator crash
            )
        except Exception as exc:
            carb.log_warn(f"[BLV] CreateReference command failed: {exc}")
            # Fallback: raw USD API
            try:
                prim = stage.DefinePrim(target, "Xform")
                prim.GetReferences().AddReference(abs_path)
            except Exception as exc2:
                carb.log_error(f"[BLV] Fallback also failed: {exc2}")
                return False

        # ── 5. Let MetricsAssembler run ────────────────────────────────
        # MetricsAssembler fires automatically on the next frame and
        # inserts unitsResolve xformOps.  We don't need to do anything.
        # Our translate write below is additive — it doesn't destroy
        # the unitsResolve ops.

        # ── 6. Restore translate only ──────────────────────────────────
        prim = stage.GetPrimAtPath(target)
        if prim.IsValid() and self._saved_translate is not None:
            self._write_translate(prim, self._saved_translate)

        # ── 7. Semantic label ──────────────────────────────────────────
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
    def _read_translate(prim: Usd.Prim) -> Gf.Vec3d:
        """Read the world-space translation from a prim.

        Uses ComputeLocalToWorldTransform to get the actual position
        regardless of what xformOps exist (including unitsResolve ops
        from MetricsAssembler).
        """
        try:
            xf = UsdGeom.Xformable(prim)
            mat = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            return Gf.Vec3d(mat.ExtractTranslation())
        except Exception:
            return Gf.Vec3d(0, 0, 0)

    @staticmethod
    def _write_translate(prim: Usd.Prim, translate: Gf.Vec3d) -> None:
        """Write ONLY the translate xformOp, preserving all other ops.

        This is the key to not breaking MetricsAssembler's corrections.
        MetricsAssembler adds ops like:
          - xformOp:scale:unitsResolve = (100, 100, 100)
          - xformOp:rotateX:unitsResolve = -90.0
        and sets xformOpOrder accordingly.

        We check if xformOp:translate already exists (it may have been
        created by DefinePrim("Xform") or by the referenced asset).
        If yes, just set its value.  If not, add it and prepend it to
        the existing op order.
        """
        try:
            xf = UsdGeom.Xformable(prim)
            if not xf:
                return

            # Check if translate op already exists
            translate_attr = prim.GetAttribute("xformOp:translate")
            if translate_attr and translate_attr.IsValid():
                # Detect precision
                type_name = str(translate_attr.GetTypeName())
                if "double" in type_name:
                    translate_attr.Set(Gf.Vec3d(translate))
                else:
                    translate_attr.Set(Gf.Vec3f(translate[0], translate[1], translate[2]))
            else:
                # No translate op yet — add one and prepend to order
                t_op = xf.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
                t_op.Set(Gf.Vec3d(translate))

                # Prepend our translate to whatever ops exist
                # (e.g., MetricsAssembler's unitsResolve ops)
                existing_ops = xf.GetOrderedXformOps()
                new_order = [t_op]
                for op in existing_ops:
                    if op.GetOpName() != "xformOp:translate":
                        new_order.append(op)
                xf.SetXformOpOrder(new_order)

        except Exception as exc:
            carb.log_warn(f"[BLV] Translate write failed on {prim.GetPath()}: {exc}")

    # ── semantic labeling ──────────────────────────────────────────────

    def _apply_semantic_label(self, stage: Usd.Stage, target_prim: Usd.Prim) -> None:
        """Apply class label to target_prim, remove from previously labeled prims."""
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
            except Exception as exc:
                carb.log_error(f"[BLV] Semantic label failed: {exc}")
