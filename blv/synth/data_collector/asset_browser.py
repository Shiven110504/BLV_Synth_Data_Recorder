"""
AssetBrowser — USD asset folder browser with semantic labeling.
===============================================================

Scans a local directory for USD files, presents them as a navigable list,
and loads each one by deleting the previous prim and creating a fresh prim
that references the new USD.

Transform management
--------------------
The spawn transform (position, orientation, scale) can be set in two ways:

1. **From Selection** — call :meth:`capture_transform_from_prim` with a
   viewport-selected prim path.  Its world transform is captured and used
   for the first asset load.
2. **Persistent across swaps** — on every :meth:`load_asset` call, the
   *current* prim's transform is read before deletion, so if the user
   moves/rotates/scales the asset in the viewport, the next asset inherits
   that updated transform.

All transforms use Isaac Sim's canonical ``reset_and_set_xform_ops`` which
sets exactly three ops (Translate, Orient, Scale) in the correct order,
eliminating xformOpOrder warnings.

Semantic labeling
-----------------
Every loaded asset receives the configured class label via
``isaacsim.core.utils.semantics``.  The label is removed from the previous
prim before being applied to the new one.
"""

from __future__ import annotations

import glob as _glob
import os
import re
from typing import List, Optional, Set, Tuple

import carb
import omni.kit.commands
import omni.usd
from pxr import Gf, Sdf, Usd, UsdGeom

from isaacsim.core.utils.xforms import reset_and_set_xform_ops


class AssetBrowser:
    """Browse and swap USD assets from a local folder.

    Parameters
    ----------
    asset_folder : str
        Initial directory to scan (can be changed later via :meth:`set_folder`).
    class_name : str
        Semantic class label applied to each loaded asset.
    parent_prim_path : str
        USD prim path under which new asset prims are created.
    """

    USD_EXTENSIONS: tuple = ("*.usd", "*.usda", "*.usdc", "*.usdz")

    def __init__(
        self,
        asset_folder: str = "",
        class_name: str = "",
        parent_prim_path: str = "/World",
    ) -> None:
        self._asset_folder: str = asset_folder
        self._class_name: str = class_name
        self._parent_prim_path: str = parent_prim_path

        self._assets: List[str] = []
        self._current_index: int = -1
        self._current_prim_path: str = ""

        # Spawn transform — updated from selection or from the current prim
        # before each swap so the next asset inherits any user adjustments.
        self._spawn_translate: Gf.Vec3d = Gf.Vec3d(0.0, 0.0, 0.0)
        self._spawn_orient: Gf.Quatd = Gf.Quatd(1.0, 0.0, 0.0, 0.0)
        self._spawn_scale: Gf.Vec3d = Gf.Vec3d(1.0, 1.0, 1.0)

        # Cache of prim paths carrying our class label.
        self._labeled_prim_paths: Set[str] = set()

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

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
    def current_prim_path(self) -> str:
        return self._current_prim_path

    @property
    def class_name(self) -> str:
        return self._class_name

    @class_name.setter
    def class_name(self, name: str) -> None:
        self._class_name = name

    @property
    def asset_folder(self) -> str:
        return self._asset_folder

    @property
    def spawn_translate(self) -> Gf.Vec3d:
        return self._spawn_translate

    @property
    def spawn_orient(self) -> Gf.Quatd:
        return self._spawn_orient

    @property
    def spawn_scale(self) -> Gf.Vec3d:
        return self._spawn_scale

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def set_folder(self, folder_path: str, class_name: Optional[str] = None) -> int:
        """Scan *folder_path* for USD files and optionally update the class name.

        Returns the number of USD files found.
        """
        folder_path = os.path.normpath(os.path.expanduser(folder_path))
        self._asset_folder = folder_path
        if class_name is not None:
            self._class_name = class_name

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

    def set_spawn_transform(
        self,
        translate: Gf.Vec3d,
        orient: Gf.Quatd,
        scale: Gf.Vec3d,
    ) -> None:
        """Directly set the spawn transform for subsequent asset loads."""
        self._spawn_translate = Gf.Vec3d(translate)
        self._spawn_orient = Gf.Quatd(orient)
        self._spawn_scale = Gf.Vec3d(scale)

    def capture_transform_from_prim(self, prim_path: str) -> bool:
        """Read world transform from *prim_path* and store as spawn transform.

        The parent prim path is NOT changed — assets are always created
        under the configured parent (default ``/World``).

        Returns ``True`` on success.
        """
        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available.")
            return False

        prim = stage.GetPrimAtPath(prim_path)
        if not prim.IsValid():
            carb.log_error(f"[BLV] Prim at '{prim_path}' is invalid.")
            return False

        translate, orient, scale = self._read_world_transform(prim)
        self._spawn_translate = translate
        self._spawn_orient = orient
        self._spawn_scale = scale

        carb.log_info(
            f"[BLV] Captured transform from {prim_path}: "
            f"t={translate}, r={orient}, s={scale}"
        )
        return True

    def read_current_prim_transform(self) -> Optional[Tuple[Gf.Vec3d, Gf.Quatd, Gf.Vec3d]]:
        """Read the current prim's local xformOps for live UI display.

        Returns ``(translate, orient, scale)`` or ``None`` if no prim is loaded.
        """
        if not self._current_prim_path:
            return None
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return None
        prim = stage.GetPrimAtPath(self._current_prim_path)
        if not prim.IsValid():
            return None
        return self._read_local_xform_ops(prim)

    def next_asset(self) -> bool:
        """Load the next asset (wraps around)."""
        if not self._assets:
            carb.log_warn("[BLV] No assets available — set a folder first.")
            return False
        next_idx = (self._current_index + 1) % len(self._assets)
        return self.load_asset(next_idx)

    def previous_asset(self) -> bool:
        """Load the previous asset (wraps around)."""
        if not self._assets:
            carb.log_warn("[BLV] No assets available — set a folder first.")
            return False
        prev_idx = (self._current_index - 1) % len(self._assets)
        return self.load_asset(prev_idx)

    def load_asset(self, index: int) -> bool:
        """Load a specific asset by index.

        Deletes the current prim, creates a new one with a USD reference,
        applies the spawn transform, and sets the semantic label.
        """
        if index < 0 or index >= len(self._assets):
            carb.log_error(f"[BLV] Asset index {index} out of range [0, {len(self._assets)}).")
            return False

        asset_path = self._assets[index]
        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available.")
            return False

        # Ensure parent prim exists
        if not stage.GetPrimAtPath(self._parent_prim_path).IsValid():
            omni.kit.commands.execute(
                "CreatePrim",
                prim_path=self._parent_prim_path,
                prim_type="Xform",
                select_new_prim=False,
            )

        # Snapshot the local xformOps we set on the current prim before
        # deleting, so the next asset inherits any user adjustments.
        # Uses _read_local_xform_ops (not world transform) to avoid
        # baking in the referenced USD's internal transforms.
        if self._current_prim_path:
            existing = stage.GetPrimAtPath(self._current_prim_path)
            if existing.IsValid():
                t, r, s = self._read_local_xform_ops(existing)
                self._spawn_translate = t
                self._spawn_orient = r
                self._spawn_scale = s

        # Clear viewport selection before mutating prims to prevent
        # Kit's manipulator from firing on an invalid SdfPath.
        try:
            omni.usd.get_context().get_selection().set_selected_prim_paths([], False)
        except Exception as exc:
            carb.log_warn(f"[BLV] Could not clear viewport selection: {exc}")

        # Delete old prim
        if self._current_prim_path:
            existing = stage.GetPrimAtPath(self._current_prim_path)
            if existing.IsValid():
                try:
                    omni.kit.commands.execute("DeletePrims", paths=[self._current_prim_path])
                except Exception as exc:
                    carb.log_error(f"[BLV] Failed to delete prim {self._current_prim_path}: {exc}")
                    return False

        # Compose new prim path
        asset_stem = os.path.splitext(os.path.basename(asset_path))[0]
        clean_name = self._sanitize_prim_name(asset_stem)
        candidate = f"{self._parent_prim_path}/{clean_name}"
        try:
            new_path = omni.usd.get_stage_next_free_path(stage, candidate, False)
        except Exception:
            new_path = candidate

        # Create prim and add reference
        try:
            omni.kit.commands.execute(
                "CreatePrim",
                prim_path=new_path,
                prim_type="Xform",
                select_new_prim=False,
            )
            omni.kit.commands.execute(
                "AddReference",
                stage=stage,
                prim_path=Sdf.Path(new_path),
                reference=Sdf.Reference(asset_path),
            )
        except Exception as exc:
            carb.log_error(f"[BLV] Failed to create reference at {new_path}: {exc}")
            return False

        prim = stage.GetPrimAtPath(new_path)
        if not prim.IsValid():
            carb.log_error(f"[BLV] New prim at {new_path} is invalid after creation.")
            return False

        # Apply transform using Isaac Sim's canonical xform utility
        try:
            reset_and_set_xform_ops(
                prim,
                self._spawn_translate,
                self._spawn_orient,
                self._spawn_scale,
            )
        except Exception as exc:
            carb.log_warn(f"[BLV] Could not set transform on {new_path}: {exc}")

        # Update tracking
        self._current_prim_path = new_path
        self._current_index = index

        # Apply semantic label
        self._apply_semantic_label(stage, prim)

        carb.log_info(
            f"[BLV] Loaded asset [{index + 1}/{len(self._assets)}] "
            f"'{os.path.basename(asset_path)}' -> {new_path}"
        )
        return True

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _derive_parent(prim_path: str) -> str:
        """Return the parent path of *prim_path*, defaulting to ``/World``."""
        if not prim_path or "/" not in prim_path.strip("/"):
            return "/World"
        parent = prim_path.rsplit("/", 1)[0]
        return parent or "/World"

    @staticmethod
    def _sanitize_prim_name(name: str) -> str:
        """Convert *name* into a valid USD prim name (alnum + underscores)."""
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
        if not cleaned or cleaned[0].isdigit():
            cleaned = "Asset_" + cleaned
        return cleaned

    @staticmethod
    def _read_world_transform(
        prim: Usd.Prim,
    ) -> Tuple[Gf.Vec3d, Gf.Quatd, Gf.Vec3d]:
        """Read world translate, orient (quaternion), and scale from *prim*.

        Uses ``Gf.Transform`` to decompose the local-to-world matrix.
        Only used for the initial "From Selection" capture on arbitrary prims.
        """
        xformable = UsdGeom.Xformable(prim)
        mat = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        xform = Gf.Transform()
        xform.SetMatrix(mat)

        translate = Gf.Vec3d(xform.GetTranslation())
        orient = Gf.Quatd(xform.GetRotation().GetQuat())
        scale = Gf.Vec3d(xform.GetScale())

        return translate, orient, scale

    @staticmethod
    def _read_local_xform_ops(
        prim: Usd.Prim,
    ) -> Tuple[Gf.Vec3d, Gf.Quatd, Gf.Vec3d]:
        """Read the local xformOp values we explicitly set on *prim*.

        Reads only ``xformOp:translate``, ``xformOp:orient``, and
        ``xformOp:scale`` — ignoring any transforms contributed by
        USD references or composition.  This prevents scale (or any
        other transform) from compounding across asset swaps.
        """
        translate = Gf.Vec3d(0.0, 0.0, 0.0)
        orient = Gf.Quatd(1.0, 0.0, 0.0, 0.0)
        scale = Gf.Vec3d(1.0, 1.0, 1.0)

        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            return translate, orient, scale

        for op in xformable.GetOrderedXformOps():
            name = op.GetOpName()
            val = op.Get()
            if val is None:
                continue
            if name == "xformOp:translate":
                translate = Gf.Vec3d(val)
            elif name == "xformOp:orient":
                orient = Gf.Quatd(val)
            elif name == "xformOp:scale":
                scale = Gf.Vec3d(val)

        return translate, orient, scale

    def _apply_semantic_label(self, stage: Usd.Stage, target_prim: Usd.Prim) -> None:
        """Apply the class label to *target_prim* and remove from previous prims."""
        try:
            from isaacsim.core.utils.semantics import add_labels, remove_labels
        except ImportError:
            carb.log_error(
                "[BLV] Could not import isaacsim.core.utils.semantics — "
                "semantic labeling unavailable."
            )
            return

        # Remove from previously labeled prims
        stale_paths = set(self._labeled_prim_paths)
        for path in stale_paths:
            if path == str(target_prim.GetPath()):
                continue
            old_prim = stage.GetPrimAtPath(path)
            if old_prim.IsValid():
                try:
                    remove_labels(old_prim, instance_name="class")
                except Exception as exc:
                    carb.log_warn(f"[BLV] Failed to remove label from {path}: {exc}")
        self._labeled_prim_paths.clear()

        # Add to target prim
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
