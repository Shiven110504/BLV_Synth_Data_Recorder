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
from pxr import Gf, Usd, UsdGeom


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

        carb.log_info(f"[BLV] AssetBrowser scanning '{folder_path}' (isdir={os.path.isdir(folder_path)})")

        # Collect and sort all matching files — flat scan, no recursion.
        # Assets are expected directly inside *folder_path* (not nested).
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

        # If the target prim already exists, snapshot its transform so we
        # can re-apply it after the swap — this lets every asset appear at
        # the position the user placed the target prim in.
        existing = stage.GetPrimAtPath(self._target_prim_path)
        if existing.IsValid():
            saved_translate, saved_rot_xyz, saved_scale = self._read_xform(existing)
        else:
            saved_translate = Gf.Vec3d(0.0, 0.0, 0.0)
            saved_rot_xyz = Gf.Vec3f(0.0, 0.0, 0.0)
            saved_scale = Gf.Vec3f(1.0, 1.0, 1.0)

        # Clear the viewport selection *before* mutating the prim.
        # If the user had a child of the referenced asset selected (e.g.
        # ``/World/TargetAsset/ElavatorRequestButtons01_Low_A``), removing
        # the target prim destroys that child, and Kit's manipulator fires
        # ``on_selection_changed`` with a now-invalid SdfPath — the
        # source of the ``Ill-formed SdfPath <>`` / ``KeyError: NoneType``
        # errors that cascade out of the viewport update loop.
        try:
            omni.usd.get_context().get_selection().set_selected_prim_paths([], False)
        except Exception as exc:
            carb.log_warn(f"[BLV] Could not clear viewport selection: {exc}")

        # Strategy: obliterate the target prim and redefine it clean,
        # *then* add the new reference.  This gives us three guarantees:
        #
        # 1. No leftover child prims from the previous reference, so
        #    ``MetricsAssemblerManager`` — which aborts ``AddReference``
        #    when "path already contains children prims" with mismatched
        #    units — always sees a fresh prim and approves the add.
        # 2. No stale xformOp attributes with the previous asset's
        #    precision, so our ``_write_xform`` won't hit the
        #    ``PrecisionFloat != double3`` error.
        # 3. Hydra receives a prim-removed + prim-added notification
        #    rather than a subtle reference-list mutation on an existing
        #    prim, which the render delegate handles cleanly and
        #    actually redraws the viewport.
        try:
            if existing.IsValid():
                stage.RemovePrim(self._target_prim_path)
            prim = stage.DefinePrim(self._target_prim_path, "Xform")
            prim.GetReferences().AddReference(asset_path)
        except Exception as exc:
            carb.log_error(
                f"[BLV] Failed to swap reference on {self._target_prim_path}: {exc}"
            )
            return False

        # Re-apply the saved xform onto the freshly-defined prim so it
        # overrides the asset's internal transforms and sits where the
        # user placed the target prim.
        self._write_xform(prim, saved_translate, saved_rot_xyz, saved_scale)

        self._current_index = index

        # Update semantic labels
        self._apply_semantic_label(stage, prim)

        # Use log_warn (visible by default in Kit's console) so the user
        # can confirm swaps are happening.
        carb.log_warn(
            f"[BLV] Loaded asset [{index + 1}/{len(self._assets)}] "
            f"'{os.path.basename(asset_path)}' → {self._target_prim_path}"
        )
        return True

    # ------------------------------------------------------------------ #
    #  Internal — Transform helpers                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _read_xform(prim: Usd.Prim):
        """Read translate / rotateXYZ / scale from *prim* if present.

        Returns a tuple of ``(Vec3d translate, Vec3f rotateXYZ, Vec3f scale)``,
        using identity defaults when an op is missing.
        """
        translate = Gf.Vec3d(0.0, 0.0, 0.0)
        rotate = Gf.Vec3f(0.0, 0.0, 0.0)
        scale = Gf.Vec3f(1.0, 1.0, 1.0)

        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            return translate, rotate, scale

        for op in xformable.GetOrderedXformOps():
            name = op.GetOpName()
            val = op.Get()
            if val is None:
                continue
            if name == "xformOp:translate":
                translate = Gf.Vec3d(val)
            elif name == "xformOp:rotateXYZ":
                rotate = Gf.Vec3f(val)
            elif name == "xformOp:scale":
                scale = Gf.Vec3f(val)
        return translate, rotate, scale

    @staticmethod
    def _write_xform(
        prim: Usd.Prim,
        translate: "Gf.Vec3d",
        rotate_xyz: "Gf.Vec3f",
        scale: "Gf.Vec3f",
    ) -> None:
        """Force-apply explicit translate/rotate/scale xformOps on *prim*.

        Works for prims whose op stack is *not* ``XformCommonAPI``-compatible
        — e.g. a referenced USDZ that defines ``xformOp:transform`` (matrix)
        or ``xformOp:orient`` (quaternion), which would make
        ``UsdGeom.XformCommonAPI`` refuse with "incompatible xformable".

        Strategy:
          1. ``ClearXformOpOrder`` on the local layer.  This removes ops
             from the order list but leaves the underlying attributes
             intact, so subsequent ``AddXformOp`` calls can reuse them.
          2. For each op we want (translate, rotateXYZ, scale), detect
             the precision of the existing underlying attribute (if any)
             so that ``AddXformOp`` does not raise
             ``PrecisionFloat != double3``.
          3. Create/reuse the op and set its value at the matching
             precision.
          4. Call ``SetXformOpOrder`` so the layer order lists exactly
             ``[translate, rotate, scale]`` — any other ops the
             reference brought in remain defined but are no longer
             applied, and the prim's local transform is determined
             purely by ours.

        Any failure is logged but swallowed — transform preservation is
        best-effort and must not block downstream steps like semantic
        labeling.
        """
        try:
            xformable = UsdGeom.Xformable(prim)
            if not xformable:
                return

            def precision_of(attr_name: str, default):
                """Return the XformOp precision that matches the existing
                attribute's typeName, or *default* if the attribute does
                not exist yet.
                """
                if not prim.HasAttribute(attr_name):
                    return default
                type_name = str(prim.GetAttribute(attr_name).GetTypeName())
                if "double" in type_name:
                    return UsdGeom.XformOp.PrecisionDouble
                if "half" in type_name:
                    return UsdGeom.XformOp.PrecisionHalf
                return UsdGeom.XformOp.PrecisionFloat

            def cast(value, precision):
                if precision == UsdGeom.XformOp.PrecisionDouble:
                    return Gf.Vec3d(value)
                # Half and Float both accept a Vec3f from python bindings
                return Gf.Vec3f(value)

            # USD's ``AddXformOp`` raises if the op name is already
            # present in ``xformOpOrder`` — which happens on every call
            # after the first.  Clearing the order here is safe: the
            # underlying attributes (xformOp:translate, etc.) are NOT
            # deleted, only removed from the local-layer order list.
            # ``AddXformOp`` then reuses those existing attributes as
            # long as precision matches — which we guarantee via
            # ``precision_of`` below.
            xformable.ClearXformOpOrder()

            # -- Translate --------------------------------------------
            t_prec = precision_of(
                "xformOp:translate", UsdGeom.XformOp.PrecisionDouble
            )
            t_op = xformable.AddTranslateOp(t_prec)
            t_op.Set(cast(translate, t_prec))

            # -- Rotate (XYZ) -----------------------------------------
            r_prec = precision_of(
                "xformOp:rotateXYZ", UsdGeom.XformOp.PrecisionFloat
            )
            r_op = xformable.AddRotateXYZOp(r_prec)
            r_op.Set(cast(rotate_xyz, r_prec))

            # -- Scale -------------------------------------------------
            s_prec = precision_of(
                "xformOp:scale", UsdGeom.XformOp.PrecisionFloat
            )
            s_op = xformable.AddScaleOp(s_prec)
            s_op.Set(cast(scale, s_prec))

            # Re-establish the explicit order after the clear.  Any
            # other ops the reference brought in (xformOp:transform
            # matrix, xformOp:orient quat, ...) remain defined as
            # attributes but are no longer applied because they're not
            # in the order list.
            xformable.SetXformOpOrder([t_op, r_op, s_op])
        except Exception as exc:
            carb.log_warn(
                f"[BLV] Could not preserve target prim transform "
                f"on {prim.GetPath()}: {exc}"
            )

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
