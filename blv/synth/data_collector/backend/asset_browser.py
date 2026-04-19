"""AssetBrowser — USD asset folder browser with semantic labeling.

Scans a local directory for USD files, presents them as a navigable
list, and loads each one by deleting the previous prim and creating a
fresh prim that references the new USD.

Refactor additions
------------------
* Optional :class:`EventBus` subscribed on construction — emits
  ``asset_transform_changed`` when the per-tick transform diff detects
  motion.  The Session wires this to :meth:`LocationManager.save_transform`.
* :meth:`clear_stage_state` — clears cached prim paths and label set
  so :class:`StageController` can wipe residual state during a
  stage swap without re-instantiating the browser.

Transform management
--------------------
1. **From Selection** — :meth:`capture_transform_from_prim` captures
   the viewport-selected prim's world transform.
2. **Persistent across swaps** — :meth:`load_asset` reads the current
   prim's local xformOps before deletion so Prev/Next inherits user
   adjustments.

All transforms use Isaac Sim's canonical ``reset_and_set_xform_ops``
which writes exactly three ops (Translate, Orient, Scale).

Semantic labeling
-----------------
Every loaded asset receives the configured class label via
``isaacsim.core.utils.semantics``.
"""

from __future__ import annotations

import glob as _glob
import os
import re
from typing import List, Optional, Set, Tuple

import carb
import omni.kit.app
import omni.kit.commands
import omni.usd
from pxr import Gf, Sdf, Usd, UsdGeom

from isaacsim.core.utils.xforms import reset_and_set_xform_ops

from .events import EventBus


# Transform-change detection thresholds.  Under these deltas we treat
# the transform as unchanged — prevents continuous emit spam from
# floating-point jitter while viewport widgets are merely focused.
_TRANSLATE_EPS: float = 1e-4  # stage units
_QUAT_EPS: float = 1e-5
_SCALE_EPS: float = 1e-5


class AssetBrowser:
    """Browse and swap USD assets from a local folder."""

    USD_EXTENSIONS: tuple = ("*.usd", "*.usda", "*.usdc", "*.usdz")
    TRANSFORM_CHANGED_EVENT: str = "asset_transform_changed"

    def __init__(
        self,
        asset_folder: str = "",
        class_name: str = "",
        parent_prim_path: str = "/World",
        bus: Optional[EventBus] = None,
    ) -> None:
        self._asset_folder: str = asset_folder
        self._class_name: str = class_name
        self._parent_prim_path: str = parent_prim_path
        self._bus: Optional[EventBus] = bus

        self._assets: List[str] = []
        self._current_index: int = -1
        self._current_prim_path: str = ""

        self._spawn_translate: Gf.Vec3d = Gf.Vec3d(0.0, 0.0, 0.0)
        self._spawn_orient: Gf.Quatd = Gf.Quatd(1.0, 0.0, 0.0, 0.0)
        self._spawn_scale: Gf.Vec3d = Gf.Vec3d(1.0, 1.0, 1.0)

        self._labeled_prim_paths: Set[str] = set()

        # Snapshot used to detect transform deltas tick-to-tick.  Stays
        # None until the first asset load so we don't emit a spurious
        # "changed" event right after spawn.
        self._last_t: Optional[Gf.Vec3d] = None
        self._last_r: Optional[Gf.Quatd] = None
        self._last_s: Optional[Gf.Vec3d] = None

        self._tick_sub = None
        if self._bus is not None:
            self._tick_sub = (
                omni.kit.app.get_app()
                .get_update_event_stream()
                .create_subscription_to_pop(
                    self._on_tick, name="blv.asset_browser.tick"
                )
            )

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def destroy(self) -> None:
        if self._tick_sub is not None:
            try:
                self._tick_sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._tick_sub = None

    def clear_stage_state(self) -> None:
        """Forget any current prim / labeled prims.

        Called by :class:`StageController` on stage close because the
        prim paths from the old stage are no longer valid.  Spawn
        transform is preserved so the next stage inherits it.
        """
        self._current_prim_path = ""
        self._current_index = -1
        self._labeled_prim_paths.clear()
        self._last_t = self._last_r = self._last_s = None

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
            return os.path.splitext(
                os.path.basename(self._assets[self._current_index])
            )[0]
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

    def set_folder(
        self, folder_path: str, class_name: Optional[str] = None
    ) -> int:
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
        self._spawn_translate = Gf.Vec3d(translate)
        self._spawn_orient = Gf.Quatd(orient)
        self._spawn_scale = Gf.Vec3d(scale)

    def capture_transform_from_prim(self, prim_path: str) -> bool:
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

    def read_current_prim_transform(
        self,
    ) -> Optional[Tuple[Gf.Vec3d, Gf.Quatd, Gf.Vec3d]]:
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
        if not self._assets:
            carb.log_warn("[BLV] No assets available — set a folder first.")
            return False
        next_idx = (self._current_index + 1) % len(self._assets)
        return self.load_asset(next_idx)

    def previous_asset(self) -> bool:
        if not self._assets:
            carb.log_warn("[BLV] No assets available — set a folder first.")
            return False
        prev_idx = (self._current_index - 1) % len(self._assets)
        return self.load_asset(prev_idx)

    def load_asset(self, index: int, preserve_transform: bool = True) -> bool:
        if index < 0 or index >= len(self._assets):
            carb.log_error(
                f"[BLV] Asset index {index} out of range [0, {len(self._assets)})."
            )
            return False

        asset_path = self._assets[index]
        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available.")
            return False

        if not stage.GetPrimAtPath(self._parent_prim_path).IsValid():
            omni.kit.commands.execute(
                "CreatePrim",
                prim_path=self._parent_prim_path,
                prim_type="Xform",
                select_new_prim=False,
            )

        if preserve_transform and self._current_prim_path:
            existing = stage.GetPrimAtPath(self._current_prim_path)
            if existing.IsValid():
                t, r, s = self._read_local_xform_ops(existing)
                self._spawn_translate = t
                self._spawn_orient = r
                self._spawn_scale = s

        try:
            omni.usd.get_context().get_selection().set_selected_prim_paths([], False)
        except Exception as exc:  # noqa: BLE001
            carb.log_warn(f"[BLV] Could not clear viewport selection: {exc}")

        if self._current_prim_path:
            existing = stage.GetPrimAtPath(self._current_prim_path)
            if existing.IsValid():
                try:
                    omni.kit.commands.execute(
                        "DeletePrims", paths=[self._current_prim_path]
                    )
                except Exception as exc:  # noqa: BLE001
                    carb.log_error(
                        f"[BLV] Failed to delete prim {self._current_prim_path}: {exc}"
                    )
                    return False

        asset_stem = os.path.splitext(os.path.basename(asset_path))[0]
        clean_name = self._sanitize_prim_name(asset_stem)
        candidate = f"{self._parent_prim_path}/{clean_name}"
        try:
            new_path = omni.usd.get_stage_next_free_path(stage, candidate, False)
        except Exception:
            new_path = candidate

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
        except Exception as exc:  # noqa: BLE001
            carb.log_error(f"[BLV] Failed to create reference at {new_path}: {exc}")
            return False

        prim = stage.GetPrimAtPath(new_path)
        if not prim.IsValid():
            carb.log_error(f"[BLV] New prim at {new_path} is invalid.")
            return False

        try:
            reset_and_set_xform_ops(
                prim,
                self._spawn_translate,
                self._spawn_orient,
                self._spawn_scale,
            )
        except Exception as exc:  # noqa: BLE001
            carb.log_warn(f"[BLV] Could not set transform on {new_path}: {exc}")

        self._current_prim_path = new_path
        self._current_index = index

        self._apply_semantic_label(stage, prim)

        # Reset snapshot so the next tick picks up the just-applied
        # transform as the baseline.  Otherwise moving an asset that
        # happens to match the previous asset's transform wouldn't emit.
        self._last_t = Gf.Vec3d(self._spawn_translate)
        self._last_r = Gf.Quatd(self._spawn_orient)
        self._last_s = Gf.Vec3d(self._spawn_scale)

        carb.log_info(
            f"[BLV] Loaded asset [{index + 1}/{len(self._assets)}] "
            f"'{os.path.basename(asset_path)}' -> {new_path}"
        )
        return True

    # ------------------------------------------------------------------ #
    #  Transform-change tick                                              #
    # ------------------------------------------------------------------ #

    def _on_tick(self, event) -> None:
        if self._bus is None or not self._current_prim_path:
            return
        snap = self.read_current_prim_transform()
        if snap is None:
            return
        t, r, s = snap

        if self._last_t is None:
            self._last_t, self._last_r, self._last_s = Gf.Vec3d(t), Gf.Quatd(r), Gf.Vec3d(s)
            return

        if self._transform_changed(t, r, s):
            self._last_t = Gf.Vec3d(t)
            self._last_r = Gf.Quatd(r)
            self._last_s = Gf.Vec3d(s)
            self._bus.emit(
                self.TRANSFORM_CHANGED_EVENT,
                translate=[t[0], t[1], t[2]],
                orient=[r.GetReal(), r.GetImaginary()[0], r.GetImaginary()[1], r.GetImaginary()[2]],
                scale=[s[0], s[1], s[2]],
            )

    def _transform_changed(
        self, t: Gf.Vec3d, r: Gf.Quatd, s: Gf.Vec3d
    ) -> bool:
        if (
            abs(t[0] - self._last_t[0]) > _TRANSLATE_EPS
            or abs(t[1] - self._last_t[1]) > _TRANSLATE_EPS
            or abs(t[2] - self._last_t[2]) > _TRANSLATE_EPS
        ):
            return True
        last_r = self._last_r
        if (
            abs(r.GetReal() - last_r.GetReal()) > _QUAT_EPS
            or abs(r.GetImaginary()[0] - last_r.GetImaginary()[0]) > _QUAT_EPS
            or abs(r.GetImaginary()[1] - last_r.GetImaginary()[1]) > _QUAT_EPS
            or abs(r.GetImaginary()[2] - last_r.GetImaginary()[2]) > _QUAT_EPS
        ):
            return True
        if (
            abs(s[0] - self._last_s[0]) > _SCALE_EPS
            or abs(s[1] - self._last_s[1]) > _SCALE_EPS
            or abs(s[2] - self._last_s[2]) > _SCALE_EPS
        ):
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _derive_parent(prim_path: str) -> str:
        if not prim_path or "/" not in prim_path.strip("/"):
            return "/World"
        parent = prim_path.rsplit("/", 1)[0]
        return parent or "/World"

    @staticmethod
    def _sanitize_prim_name(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
        if not cleaned or cleaned[0].isdigit():
            cleaned = "Asset_" + cleaned
        return cleaned

    @staticmethod
    def _read_world_transform(
        prim: Usd.Prim,
    ) -> Tuple[Gf.Vec3d, Gf.Quatd, Gf.Vec3d]:
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

    def _apply_semantic_label(
        self, stage: Usd.Stage, target_prim: Usd.Prim
    ) -> None:
        try:
            from isaacsim.core.utils.semantics import add_labels, remove_labels
        except ImportError:
            carb.log_error(
                "[BLV] Could not import isaacsim.core.utils.semantics — "
                "semantic labeling unavailable."
            )
            return

        stale_paths = set(self._labeled_prim_paths)
        for path in stale_paths:
            if path == str(target_prim.GetPath()):
                continue
            old_prim = stage.GetPrimAtPath(path)
            if old_prim.IsValid():
                try:
                    remove_labels(old_prim, instance_name="class")
                except Exception as exc:  # noqa: BLE001
                    carb.log_warn(f"[BLV] Failed to remove label from {path}: {exc}")
        self._labeled_prim_paths.clear()

        if self._class_name:
            try:
                add_labels(
                    target_prim, labels=[self._class_name], instance_name="class"
                )
                self._labeled_prim_paths.add(str(target_prim.GetPath()))
                carb.log_info(
                    f"[BLV] Semantic label '{self._class_name}' applied to "
                    f"{target_prim.GetPath()}"
                )
            except Exception as exc:  # noqa: BLE001
                carb.log_error(
                    f"[BLV] Failed to add semantic label to "
                    f"{target_prim.GetPath()}: {exc}"
                )
