"""
GamepadCameraController — FPS camera via XInput gamepad (Logitech F710).

Controls:
    Left stick      Forward / back / strafe
    Right stick     Look (yaw / pitch)
    Right trigger   Move up
    Left trigger    Move down
    D-pad up/down   Increase / decrease move speed
    Left bumper     Toggle slow mode (0.25x)

How it works:
    1. Disable Kit's built-in gamepad camera so we own the input.
    2. On each gamepad event, store the raw [0,1] axis value in a dict.
       (GamepadInput fires LEFT_STICK_UP and LEFT_STICK_DOWN as SEPARATE
        events — they are NOT a single -1..+1 axis.)
    3. Every frame, compute signed axes, update yaw/pitch/position, write
       to the camera prim's xformOps.

Camera prim uses xformOp:translate (Vec3d) + xformOp:rotateYXZ (Vec3f).
USD convention: Y-up, -Z forward.
"""

from __future__ import annotations
import math
from typing import Dict, List, Optional

import carb
import carb.input
import carb.settings
import omni.appwindow
import omni.kit.app
import omni.usd
from pxr import Gf, Usd, UsdGeom


class GamepadCameraController:
    """FPS-style camera driven by an XInput gamepad."""

    # Defaults — tune these or override via constructor / UI sliders
    DEFAULT_MOVE_SPEED = 60.0   # m/s
    DEFAULT_LOOK_SPEED = 30.0   # deg/s  (lowered — was too fast)
    DEAD_ZONE          = 0.15
    SPEED_STEP         = 5.0    # m/s per D-pad press
    SLOW_FACTOR        = 0.25
    DEFAULT_FOCAL_LENGTH = 28.0

    def __init__(self, camera_prim_path: str = "/World/BLV_Camera",
                 move_speed: Optional[float] = None,
                 look_speed: Optional[float] = None) -> None:

        self._camera_path = camera_prim_path
        self._move_speed  = move_speed or self.DEFAULT_MOVE_SPEED
        self._look_speed  = look_speed or self.DEFAULT_LOOK_SPEED
        self._enabled     = False
        self._slow_mode   = False

        # Camera state
        self._yaw   = 0.0           # degrees
        self._pitch = 0.0           # degrees
        self._pos   = Gf.Vec3d(0, 0, 0)

        # Raw gamepad values — one key per GamepadInput enum member
        self._raw: Dict[int, float] = {}

        # Carb handles (must stay alive to avoid GC dropping callbacks)
        self._input   = carb.input.acquire_input_interface()
        self._gamepad = omni.appwindow.get_default_app_window().get_gamepad(0)
        self._gp_sub     = None
        self._update_sub = None

    # ── public ──────────────────────────────────────────────────────────

    def enable(self) -> None:
        if self._enabled:
            return
        carb.settings.get_settings().set_bool(
            "/persistent/app/omniverse/gamepadCameraControl", False)

        self._ensure_camera()
        self._read_pose()
        self._raw.clear()

        self._gp_sub = self._input.subscribe_to_gamepad_events(
            self._gamepad, self._on_gp_event)
        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update, name="blv.gp_cam"))
        self._enabled = True
        carb.log_info(f"[BLV] Gamepad enabled  cam={self._camera_path}")

    def disable(self) -> None:
        if not self._enabled:
            return
        if self._gp_sub is not None:
            self._input.unsubscribe_to_gamepad_events(self._gamepad, self._gp_sub)
            self._gp_sub = None
        self._update_sub = None
        self._enabled = False
        carb.settings.get_settings().set_bool(
            "/persistent/app/omniverse/gamepadCameraControl", True)
        carb.log_info("[BLV] Gamepad disabled")

    def destroy(self) -> None:
        self.disable()

    # ── properties (keep the interface the other modules expect) ────────

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
            self._ensure_camera()
            self._read_pose()

    @property
    def move_speed(self) -> float:
        return self._move_speed

    @move_speed.setter
    def move_speed(self, v: float) -> None:
        self._move_speed = max(0.1, v)

    @property
    def look_speed(self) -> float:
        return self._look_speed

    @look_speed.setter
    def look_speed(self, v: float) -> None:
        self._look_speed = max(1.0, v)

    @property
    def slow_mode(self) -> bool:
        return self._slow_mode

    # ── pose helpers (used by trajectory recorder/player) ──────────────

    def get_pose(self) -> Dict[str, List[float]]:
        return {
            "position": [self._pos[0], self._pos[1], self._pos[2]],
            "rotation": [self._pitch, self._yaw, 0.0],
        }

    def set_pose(self, position: List[float], rotation: List[float]) -> None:
        self._pos   = Gf.Vec3d(*position)
        self._pitch = rotation[0]
        self._yaw   = rotation[1]
        self._write_usd()

    # ── internals ──────────────────────────────────────────────────────

    def _ensure_camera(self) -> None:
        """Create camera prim if needed and set viewport to it."""
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            cam = UsdGeom.Camera.Define(stage, self._camera_path)
            cam.GetFocalLengthAttr().Set(self.DEFAULT_FOCAL_LENGTH)
            x = UsdGeom.Xformable(cam.GetPrim())
            x.ClearXformOpOrder()
            x.AddTranslateOp()      # xformOp:translate
            x.AddRotateYXZOp()      # xformOp:rotateYXZ
        try:
            from omni.kit.viewport.utility import get_active_viewport
            vp = get_active_viewport()
            if vp:
                vp.camera_path = self._camera_path
        except Exception:
            pass

    def _read_pose(self) -> None:
        """Sync internal state from the camera prim (so we don't snap to origin)."""
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return

        mat = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        self._pos = Gf.Vec3d(mat.ExtractTranslation())

        rot = mat.ExtractRotation()
        decomp = rot.Decompose(Gf.Vec3d(0,1,0), Gf.Vec3d(1,0,0), Gf.Vec3d(0,0,1))
        self._yaw   = decomp[0]
        self._pitch = decomp[1]

    def _write_usd(self) -> None:
        """Push position + rotation to the camera prim."""
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return
        t = prim.GetAttribute("xformOp:translate")
        r = prim.GetAttribute("xformOp:rotateYXZ")
        if t:
            t.Set(self._pos)
        if r:
            r.Set(Gf.Vec3f(self._pitch, self._yaw, 0.0))

    # ── gamepad event → store raw axis values ──────────────────────────

    def _on_gp_event(self, event, *args) -> bool:
        G   = carb.input.GamepadInput
        val = event.value
        inp = event.input

        # dead-zone
        if abs(val) < self.DEAD_ZONE:
            val = 0.0

        # analog axes — just store the raw [0,1] value
        if inp in (G.LEFT_STICK_RIGHT, G.LEFT_STICK_LEFT,
                   G.LEFT_STICK_UP,    G.LEFT_STICK_DOWN,
                   G.RIGHT_STICK_RIGHT,G.RIGHT_STICK_LEFT,
                   G.RIGHT_STICK_UP,   G.RIGHT_STICK_DOWN,
                   G.LEFT_TRIGGER,     G.RIGHT_TRIGGER):
            self._raw[inp] = val

        # D-pad speed
        if inp == G.DPAD_UP and val > 0.5:
            self._move_speed += self.SPEED_STEP
        elif inp == G.DPAD_DOWN and val > 0.5:
            self._move_speed = max(0.1, self._move_speed - self.SPEED_STEP)
        # Slow-mode toggle
        elif inp == G.LEFT_SHOULDER and val > 0.5:
            self._slow_mode = not self._slow_mode

        return True

    # ── per-frame update → read axes, move camera ─────────────────────

    def _on_update(self, event) -> None:
        if not self._enabled:
            return

        try:
            dt = event.payload["dt"]
        except Exception:
            dt = 1.0 / 60.0

        G  = carb.input.GamepadInput
        ri = self._raw

        # ---- signed axes ----
        fwd      = ri.get(G.LEFT_STICK_UP,    0) - ri.get(G.LEFT_STICK_DOWN,  0)
        strafe   = ri.get(G.LEFT_STICK_RIGHT, 0) - ri.get(G.LEFT_STICK_LEFT,  0)
        yaw_in   = ri.get(G.RIGHT_STICK_RIGHT,0) - ri.get(G.RIGHT_STICK_LEFT, 0)
        pitch_in = ri.get(G.RIGHT_STICK_UP,   0) - ri.get(G.RIGHT_STICK_DOWN, 0)
        up_in    = ri.get(G.RIGHT_TRIGGER,     0) - ri.get(G.LEFT_TRIGGER,     0)

        # ---- look ----
        self._yaw   -= yaw_in   * self._look_speed * dt   # stick-right → yaw decreases
        self._pitch += pitch_in * self._look_speed * dt   # stick-up    → pitch increases

        # ---- movement (ground-plane FPS style) ----
        speed   = self._move_speed * (self.SLOW_FACTOR if self._slow_mode else 1.0)
        yaw_rad = math.radians(self._yaw)

        forward = Gf.Vec3d( math.sin(yaw_rad), 0, -math.cos(yaw_rad))
        right   = Gf.Vec3d( math.cos(yaw_rad), 0,  math.sin(yaw_rad))
        up      = Gf.Vec3d(0, 1, 0)

        self._pos += forward * (fwd    * speed * dt)
        self._pos += right   * (strafe * speed * dt)
        self._pos += up      * (up_in  * speed * dt)

        self._write_usd()
