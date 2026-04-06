"""
GamepadCameraController — gamepad-driven camera translation.
=============================================================

Left stick: forward/back + strafe (horizontal plane).
Triggers:   vertical up/down.
D-pad:      speed adjustment.

Movement axes are derived from the camera's world-space orientation at
enable time, projected onto the stage's horizontal plane, so the controller
works regardless of the stage up-axis (Y-up or Z-up).

Designed for the Logitech F710 in XInput mode (Xbox-compatible).
"""

from __future__ import annotations

from typing import Dict, List, Optional

import carb
import carb.input
import carb.settings
import omni.appwindow
import omni.kit.app
import omni.usd
from pxr import Gf, Usd, UsdGeom


def _dot(a: Gf.Vec3d, b: Gf.Vec3d) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class GamepadCameraController:
    """Camera controller driven by an XInput gamepad (translation only)."""

    DEFAULT_MOVE_SPEED: float = 50.0
    DEFAULT_LOOK_SPEED: float = 45.0
    DEAD_ZONE: float = 0.15
    SPEED_STEP: float = 5.0
    MIN_MOVE_SPEED: float = 0.1
    MIN_LOOK_SPEED: float = 1.0
    DEFAULT_FOCAL_LENGTH: float = 28.0

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        camera_prim_path: str = "/World/BLV_Camera",
        move_speed: Optional[float] = None,
        look_speed: Optional[float] = None,
    ) -> None:
        settings = carb.settings.get_settings()
        _ext = "exts.blv.synth.data_collector"

        self._camera_path: str = camera_prim_path
        self._move_speed: float = move_speed or settings.get_as_float(
            f"/{_ext}/default_move_speed"
        ) or self.DEFAULT_MOVE_SPEED
        self._look_speed: float = look_speed or settings.get_as_float(
            f"/{_ext}/default_look_speed"
        ) or self.DEFAULT_LOOK_SPEED

        self._enabled: bool = False

        # Camera state (Euler angles preserved for trajectory round-trip)
        self._yaw: float = 0.0
        self._pitch: float = 0.0
        self._position: Gf.Vec3d = Gf.Vec3d(0.0, 0.0, 0.0)

        # Movement axes — computed from camera orientation at enable time
        self._fwd_axis: Gf.Vec3d = Gf.Vec3d(1.0, 0.0, 0.0)
        self._right_axis: Gf.Vec3d = Gf.Vec3d(0.0, 1.0, 0.0)
        self._up_axis: Gf.Vec3d = Gf.Vec3d(0.0, 0.0, 1.0)

        # Raw gamepad inputs: each GamepadInput enum → [0, 1] value
        self._raw_inputs: Dict[int, float] = {}

        # Carb input handles
        self._input: carb.input.IInput = carb.input.acquire_input_interface()
        self._gamepad = omni.appwindow.get_default_app_window().get_gamepad(0)
        self._gp_sub = None
        self._update_sub = None

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def enable(self) -> None:
        if self._enabled:
            carb.log_warn("[BLV] GamepadCameraController already enabled.")
            return

        carb.settings.get_settings().set_bool(
            "/persistent/app/omniverse/gamepadCameraControl", False
        )

        self._ensure_camera_prim()
        self._read_camera_pose()
        self._compute_movement_axes()
        self._raw_inputs.clear()

        self._gp_sub = self._input.subscribe_to_gamepad_events(
            self._gamepad, self._on_gamepad_event
        )
        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update, name="blv.gamepad_camera")
        )

        self._enabled = True
        carb.log_info(f"[BLV] GamepadCameraController enabled  camera={self._camera_path}")

    def disable(self) -> None:
        if not self._enabled:
            return

        if self._gp_sub is not None:
            self._input.unsubscribe_to_gamepad_events(self._gamepad, self._gp_sub)
            self._gp_sub = None

        self._update_sub = None
        self._enabled = False

        carb.settings.get_settings().set_bool(
            "/persistent/app/omniverse/gamepadCameraControl", True
        )
        carb.log_info("[BLV] GamepadCameraController disabled.")

    def destroy(self) -> None:
        self.disable()

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def camera_path(self) -> str:
        return self._camera_path

    @camera_path.setter
    def camera_path(self, path: str) -> None:
        self._camera_path = path
        if self._enabled:
            self._ensure_camera_prim()
            self._read_camera_pose()
            self._compute_movement_axes()

    @property
    def move_speed(self) -> float:
        return self._move_speed

    @move_speed.setter
    def move_speed(self, val: float) -> None:
        self._move_speed = max(self.MIN_MOVE_SPEED, val)

    @property
    def look_speed(self) -> float:
        return self._look_speed

    @look_speed.setter
    def look_speed(self, val: float) -> None:
        self._look_speed = max(self.MIN_LOOK_SPEED, val)

    # ------------------------------------------------------------------ #
    #  Pose helpers (used by trajectory recorder / player)                #
    # ------------------------------------------------------------------ #

    def get_pose(self) -> Dict[str, List[float]]:
        return {
            "position": [self._position[0], self._position[1], self._position[2]],
            "rotation": [self._pitch, self._yaw, 0.0],
        }

    def set_pose(self, position: List[float], rotation: List[float]) -> None:
        self._position = Gf.Vec3d(*position)
        self._pitch = rotation[0]
        self._yaw = rotation[1]
        self._apply_pose_to_usd()

    # ------------------------------------------------------------------ #
    #  Internal — Camera prim management                                  #
    # ------------------------------------------------------------------ #

    def _ensure_camera_prim(self) -> None:
        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available.")
            return

        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            cam = UsdGeom.Camera.Define(stage, self._camera_path)
            cam.GetFocalLengthAttr().Set(self.DEFAULT_FOCAL_LENGTH)
            xformable = UsdGeom.Xformable(cam.GetPrim())
            xformable.ClearXformOpOrder()
            xformable.AddTranslateOp()
            xformable.AddRotateYXZOp()
            carb.log_info(
                f"[BLV] Created camera at {self._camera_path} "
                f"(focal={self.DEFAULT_FOCAL_LENGTH}mm)"
            )

        try:
            from omni.kit.viewport.utility import get_active_viewport

            viewport = get_active_viewport()
            if viewport is not None:
                viewport.camera_path = self._camera_path
        except Exception as exc:
            carb.log_warn(f"[BLV] Could not set viewport camera: {exc}")

    def _read_camera_pose(self) -> None:
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return

        xformable = UsdGeom.Xformable(prim)
        world_mat = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        self._position = Gf.Vec3d(world_mat.ExtractTranslation())

        rotation = world_mat.ExtractRotation()
        decomp = rotation.Decompose(
            Gf.Vec3d(0, 1, 0),  # yaw
            Gf.Vec3d(1, 0, 0),  # pitch
            Gf.Vec3d(0, 0, 1),  # roll
        )
        self._yaw = decomp[0]
        self._pitch = decomp[1]

    def _compute_movement_axes(self) -> None:
        """Derive forward/right/up movement axes from the camera's orientation,
        projected onto the stage's horizontal plane."""
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return

        # World up from stage settings
        up_axis = UsdGeom.GetStageUpAxis(stage)
        if up_axis == UsdGeom.Tokens.z:
            self._up_axis = Gf.Vec3d(0, 0, 1)
        else:
            self._up_axis = Gf.Vec3d(0, 1, 0)

        # Camera world transform (row-vector convention: row i = local axis i)
        xformable = UsdGeom.Xformable(prim)
        mat = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())

        # Camera forward = -Z in local space = negated row 2
        cam_fwd = Gf.Vec3d(-mat[2][0], -mat[2][1], -mat[2][2])
        cam_right = Gf.Vec3d(mat[0][0], mat[0][1], mat[0][2])

        # Project onto horizontal plane (perpendicular to world up)
        up = self._up_axis
        fwd_h = cam_fwd - up * _dot(cam_fwd, up)
        right_h = cam_right - up * _dot(cam_right, up)

        if fwd_h.GetLength() > 1e-6:
            self._fwd_axis = fwd_h.GetNormalized()
        else:
            # Camera pointing straight up/down — pick arbitrary forward
            self._fwd_axis = (
                Gf.Vec3d(1, 0, 0) if up_axis == UsdGeom.Tokens.z
                else Gf.Vec3d(0, 0, -1)
            )

        if right_h.GetLength() > 1e-6:
            self._right_axis = right_h.GetNormalized()
        else:
            # Derive right from cross(up, forward)
            f = self._fwd_axis
            self._right_axis = Gf.Vec3d(
                up[1] * f[2] - up[2] * f[1],
                up[2] * f[0] - up[0] * f[2],
                up[0] * f[1] - up[1] * f[0],
            ).GetNormalized()

        carb.log_info(
            f"[BLV] Movement axes  fwd={self._fwd_axis}  "
            f"right={self._right_axis}  up={self._up_axis}"
        )

    # ------------------------------------------------------------------ #
    #  Internal — Gamepad event handler                                   #
    # ------------------------------------------------------------------ #

    def _on_gamepad_event(self, event, *args) -> bool:
        val: float = event.value
        inp = event.input
        G = carb.input.GamepadInput

        if abs(val) < self.DEAD_ZONE:
            val = 0.0

        _analog = {
            G.LEFT_STICK_RIGHT, G.LEFT_STICK_LEFT,
            G.LEFT_STICK_UP, G.LEFT_STICK_DOWN,
            G.LEFT_TRIGGER, G.RIGHT_TRIGGER,
        }
        if inp in _analog:
            self._raw_inputs[inp] = val

        # D-pad speed adjustment
        if inp == G.DPAD_UP and val > 0.5:
            self._move_speed += self.SPEED_STEP
            carb.log_info(f"[BLV] Move speed → {self._move_speed:.1f} m/s")
        elif inp == G.DPAD_DOWN and val > 0.5:
            self._move_speed = max(self.MIN_MOVE_SPEED, self._move_speed - self.SPEED_STEP)
            carb.log_info(f"[BLV] Move speed → {self._move_speed:.1f} m/s")

        return True

    # ------------------------------------------------------------------ #
    #  Internal — Per-frame update                                        #
    # ------------------------------------------------------------------ #

    def _on_update(self, event) -> None:
        if not self._enabled:
            return

        try:
            dt: float = event.payload["dt"]
        except Exception:
            dt = 1.0 / 60.0

        G = carb.input.GamepadInput
        ri = self._raw_inputs
        speed = self._move_speed

        # Left stick: forward/back + strafe
        fwd = ri.get(G.LEFT_STICK_UP, 0.0) - ri.get(G.LEFT_STICK_DOWN, 0.0)
        strafe = ri.get(G.LEFT_STICK_RIGHT, 0.0) - ri.get(G.LEFT_STICK_LEFT, 0.0)

        # Triggers: vertical
        vert = ri.get(G.RIGHT_TRIGGER, 0.0) - ri.get(G.LEFT_TRIGGER, 0.0)

        move = Gf.Vec3d(0.0, 0.0, 0.0)
        move += self._fwd_axis * (fwd * speed * dt)
        move += self._right_axis * (strafe * speed * dt)
        move += self._up_axis * (vert * speed * dt)

        self._position += move
        self._apply_pose_to_usd()

    def _apply_pose_to_usd(self) -> None:
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return

        translate_attr = prim.GetAttribute("xformOp:translate")
        rotate_attr = prim.GetAttribute("xformOp:rotateYXZ")

        if translate_attr:
            translate_attr.Set(self._position)
        if rotate_attr:
            rotate_attr.Set(Gf.Vec3f(self._pitch, self._yaw, 0.0))
