"""
GamepadCameraController — FPS-style camera control via XInput gamepad.
======================================================================

Designed for the **Logitech F710** in XInput mode (behaves identically to an
Xbox controller).

Controls
--------
    Left stick          Forward / back / strafe
    Right stick         Look (yaw / pitch)
    Right trigger       Move up
    Left trigger        Move down
    D-pad up / down     Increase / decrease move speed
    Left bumper         Toggle slow mode (0.25×)
    X button            Toggle trajectory recording (start / stop & save)

Coordinate system
-----------------
Isaac Sim uses **Z-up**:  X = right, Y = forward, Z = up.

The camera prim uses three ordered xformOps:
    xformOp:translate   Gf.Vec3d   position
    xformOp:rotateZ     float      yaw   (rotation around world Z / up)
    xformOp:rotateX     float      pitch (+90° base so 0° pitch = horizontal)

Implementation notes
--------------------
* Kit's built-in gamepad camera (``/persistent/app/omniverse/
  gamepadCameraControl``) is disabled while this controller is active.
* ``GamepadInput`` axes fire as **separate events** with absolute values in
  ``[0, 1]``.  LEFT_STICK_UP and LEFT_STICK_DOWN are NOT a single −1…+1 axis.
* Subscription handles are kept as instance attributes to prevent GC from
  silently dropping callbacks.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional

import carb
import carb.input
import carb.settings
import omni.appwindow
import omni.kit.app
import omni.usd
from pxr import Gf, Usd, UsdGeom


class GamepadCameraController:
    """FPS-style camera controller driven by an XInput gamepad."""

    # ------------------------------------------------------------------ #
    #  Constants                                                          #
    # ------------------------------------------------------------------ #
    DEFAULT_MOVE_SPEED: float = 5.0      # metres / second
    DEFAULT_LOOK_SPEED: float = 60.0     # degrees / second
    DEAD_ZONE: float = 0.15
    SPEED_STEP: float = 2.0              # m/s per D-pad press
    SLOW_FACTOR: float = 0.25
    MIN_MOVE_SPEED: float = 0.1
    MIN_LOOK_SPEED: float = 1.0

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        camera_prim_path: str = "/World/BLV_Camera",
        move_speed: Optional[float] = None,
        look_speed: Optional[float] = None,
        focal_length: Optional[float] = None,
    ) -> None:
        settings = carb.settings.get_settings()
        _ext = "exts.blv.synth.data_collector"

        self._camera_path: str = camera_prim_path
        self._move_speed: float = (
            move_speed
            or settings.get_as_float(f"/{_ext}/default_move_speed")
            or self.DEFAULT_MOVE_SPEED
        )
        self._look_speed: float = (
            look_speed
            or settings.get_as_float(f"/{_ext}/default_look_speed")
            or self.DEFAULT_LOOK_SPEED
        )
        # Focal length in mm. USD stores it in "tenths of a scene unit" but
        # UsdGeom.Camera.GetFocalLengthAttr conventionally takes mm directly
        # in Isaac Sim's default cm-scaled stage.
        self._focal_length: Optional[float] = focal_length

        self._enabled: bool = False
        self._slow_mode: bool = False

        # Optional callback fired when the X button is pressed.
        # Wired by the UI so the user can start / stop trajectory recording
        # without letting go of the gamepad.
        self.record_toggle_callback: Optional[Callable[[], None]] = None

        # Camera state — Z-up Euler angles in degrees
        self._yaw: float = 0.0
        self._pitch: float = 0.0
        self._position: Gf.Vec3d = Gf.Vec3d(0.0, 0.0, 0.0)

        # Stage units-per-meter scaler. Isaac Sim's default stage is cm-scaled
        # (metersPerUnit = 0.01 → 100 units per meter), so move_speed expressed
        # in m/s must be multiplied by this factor when adding to _position.
        # Refreshed in _ensure_camera_prim() once a stage is available.
        self._units_per_meter: float = 100.0

        # Raw half-axis values keyed by GamepadInput enum member.
        # Each stick direction is stored independently; signed axes are
        # computed per-frame in _on_update.
        self._raw_inputs: Dict[int, float] = {}

        # Carb handles — prevent GC from dropping callbacks
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
        carb.log_info(
            f"[BLV] GamepadCameraController enabled  camera={self._camera_path}"
        )

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

    @property
    def focal_length(self) -> Optional[float]:
        return self._focal_length

    @focal_length.setter
    def focal_length(self, val: Optional[float]) -> None:
        self._focal_length = val
        # Apply immediately if camera already exists
        if val is not None:
            stage = omni.usd.get_context().get_stage()
            if stage is not None:
                prim = stage.GetPrimAtPath(self._camera_path)
                if prim.IsValid():
                    try:
                        UsdGeom.Camera(prim).GetFocalLengthAttr().Set(float(val))
                        carb.log_info(f"[BLV] Camera focal length → {val} mm")
                    except Exception as exc:
                        carb.log_warn(f"[BLV] Failed to update focal length: {exc}")

    @property
    def slow_mode(self) -> bool:
        return self._slow_mode

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
        """Create the camera prim if needed with Z-up rotation ops, and
        point the active viewport at it."""
        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available.")
            return

        # Cache stage units-per-meter so movement in m/s is honest regardless
        # of whether the stage is cm-scaled (default) or m-scaled.
        try:
            mpu = UsdGeom.GetStageMetersPerUnit(stage)
            if mpu and mpu > 0.0:
                self._units_per_meter = 1.0 / mpu
        except Exception as exc:
            carb.log_warn(f"[BLV] Could not read stage metersPerUnit: {exc}")

        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            cam = UsdGeom.Camera.Define(stage, self._camera_path)
            prim = cam.GetPrim()
            carb.log_info(f"[BLV] Created camera prim at {self._camera_path}")
        else:
            cam = UsdGeom.Camera(prim)

        # Apply focal length if configured
        if self._focal_length is not None and cam:
            try:
                cam.GetFocalLengthAttr().Set(float(self._focal_length))
                carb.log_info(
                    f"[BLV] Camera focal length set to {self._focal_length} mm"
                )
            except Exception as exc:
                carb.log_warn(f"[BLV] Failed to set focal length: {exc}")

        # Required xformOps for a Z-up FPS camera:
        #   xformOp:translate  → position
        #   xformOp:rotateZ    → yaw (around world up)
        #   xformOp:rotateX    → pitch (+90° base = horizontal)
        xformable = UsdGeom.Xformable(prim)
        ops = xformable.GetOrderedXformOps()
        op_names = [str(op.GetOpName()) for op in ops]

        if op_names != ["xformOp:translate", "xformOp:rotateZ", "xformOp:rotateX"]:
            world_mat = xformable.ComputeLocalToWorldTransform(
                Usd.TimeCode.Default()
            )
            old_pos = Gf.Vec3d(world_mat.ExtractTranslation())

            xformable.ClearXformOpOrder()
            xformable.AddTranslateOp()
            xformable.AddRotateZOp()
            xformable.AddRotateXOp()

            prim.GetAttribute("xformOp:translate").Set(old_pos)
            prim.GetAttribute("xformOp:rotateZ").Set(0.0)
            prim.GetAttribute("xformOp:rotateX").Set(90.0)
            carb.log_info(
                f"[BLV] Configured Z-up rotation ops on {self._camera_path}"
            )

        try:
            from omni.kit.viewport.utility import get_active_viewport

            viewport = get_active_viewport()
            if viewport is not None:
                viewport.camera_path = self._camera_path
        except Exception:
            pass

    def _read_camera_pose(self) -> None:
        """Sync internal state from the camera prim so we don't snap to origin."""
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return

        translate_attr = prim.GetAttribute("xformOp:translate")
        if translate_attr and translate_attr.Get() is not None:
            self._position = Gf.Vec3d(translate_attr.Get())

        yaw_attr = prim.GetAttribute("xformOp:rotateZ")
        pitch_attr = prim.GetAttribute("xformOp:rotateX")

        if yaw_attr and yaw_attr.Get() is not None:
            self._yaw = float(yaw_attr.Get())
        if pitch_attr and pitch_attr.Get() is not None:
            # USD stores (90 + pitch), so pitch = stored − 90
            self._pitch = float(pitch_attr.Get()) - 90.0

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
            G.RIGHT_STICK_RIGHT, G.RIGHT_STICK_LEFT,
            G.RIGHT_STICK_UP, G.RIGHT_STICK_DOWN,
            G.LEFT_TRIGGER, G.RIGHT_TRIGGER,
        }

        if inp in _analog:
            self._raw_inputs[inp] = val

        if inp == G.DPAD_UP and val > 0.5:
            self._move_speed += self.SPEED_STEP
            carb.log_info(f"[BLV] Move speed → {self._move_speed:.1f} m/s")
        elif inp == G.DPAD_DOWN and val > 0.5:
            self._move_speed = max(self.MIN_MOVE_SPEED, self._move_speed - self.SPEED_STEP)
            carb.log_info(f"[BLV] Move speed → {self._move_speed:.1f} m/s")
        elif inp == G.LEFT_SHOULDER and val > 0.5:
            self._slow_mode = not self._slow_mode
            carb.log_info(f"[BLV] Slow mode {'ON' if self._slow_mode else 'OFF'}")
        elif inp == G.X and val > 0.5:
            if self.record_toggle_callback is not None:
                try:
                    self.record_toggle_callback()
                except Exception as exc:
                    carb.log_error(f"[BLV] record_toggle_callback raised: {exc}")

        return True

    # ------------------------------------------------------------------ #
    #  Internal — Per-frame update                                        #
    # ------------------------------------------------------------------ #

    def _on_update(self, event) -> None:
        """Apply accumulated gamepad state to the camera each frame.

        Axis geometry (Z-up, yaw = rotateZ, CCW positive):
            The USD transform is T · Rz(yaw) · Rx(90+pitch).
            Camera local -Z after Rx(90°) is (0, 1, 0).
            After Rz(yaw): forward = (-sin(yaw), cos(yaw), 0).

            yaw = 0    → forward = (0, 1, 0)  = +Y  ✓
            yaw = -90  → forward = (1, 0, 0)  = +X  (turned right) ✓

            forward = (-sin(yaw),  cos(yaw), 0)   at yaw=0: (0, 1, 0) = +Y ✓
            right   = ( cos(yaw),  sin(yaw), 0)   at yaw=0: (1, 0, 0) = +X ✓
            up      = (0, 0, 1)                    always +Z              ✓

        Stick-to-action mapping (natural FPS):
            left stick up    → move forward      (fwd > 0)
            left stick right → strafe right       (strafe > 0)
            right stick right → turn right        (yaw decreases / CW)
            right stick up   → look up            (pitch increases)
            right trigger    → move up            (+Z)
            left trigger     → move down          (−Z)
        """
        if not self._enabled:
            return

        try:
            dt: float = event.payload["dt"]
        except Exception:
            dt = 1.0 / 60.0

        G = carb.input.GamepadInput
        ri = self._raw_inputs

        # ---- Signed axes from independent half-axis values ----
        # Each value is in [0, 1].  Positive direction minus negative direction
        # gives a signed value in [−1, +1].
        fwd    = ri.get(G.LEFT_STICK_UP, 0.0)    - ri.get(G.LEFT_STICK_DOWN, 0.0)
        strafe = ri.get(G.LEFT_STICK_RIGHT, 0.0) - ri.get(G.LEFT_STICK_LEFT, 0.0)
        yaw_in = ri.get(G.RIGHT_STICK_RIGHT, 0.0) - ri.get(G.RIGHT_STICK_LEFT, 0.0)
        pitch_in = ri.get(G.RIGHT_STICK_UP, 0.0) - ri.get(G.RIGHT_STICK_DOWN, 0.0)
        vert   = ri.get(G.RIGHT_TRIGGER, 0.0)    - ri.get(G.LEFT_TRIGGER, 0.0)

        # ---- Look ----
        # Right stick right (yaw_in > 0) → turn right → yaw decreases (CW)
        self._yaw   -= yaw_in  * self._look_speed * dt
        # Right stick up (pitch_in > 0) → look up → pitch increases
        self._pitch += pitch_in * self._look_speed * dt

        # ---- Movement (ground-plane FPS style, Z-up) ----
        # move_speed is m/s; multiply by units-per-meter so the displacement
        # is expressed in stage units (cm by default in Isaac Sim).
        speed = (
            self._move_speed
            * (self.SLOW_FACTOR if self._slow_mode else 1.0)
            * self._units_per_meter
        )
        yaw_rad = math.radians(self._yaw)

        forward = Gf.Vec3d(-math.sin(yaw_rad), math.cos(yaw_rad), 0.0)
        right   = Gf.Vec3d( math.cos(yaw_rad), math.sin(yaw_rad), 0.0)
        up      = Gf.Vec3d(0.0, 0.0, 1.0)

        self._position += forward * (fwd    * speed * dt)
        self._position += right   * (strafe * speed * dt)
        self._position += up      * (vert   * speed * dt)

        self._apply_pose_to_usd()

    def _apply_pose_to_usd(self) -> None:
        """Write position + rotation to the camera prim's xformOps."""
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return

        translate_attr = prim.GetAttribute("xformOp:translate")
        yaw_attr = prim.GetAttribute("xformOp:rotateZ")
        pitch_attr = prim.GetAttribute("xformOp:rotateX")

        if translate_attr:
            translate_attr.Set(self._position)
        if yaw_attr:
            yaw_attr.Set(float(self._yaw))
        if pitch_attr:
            # +90° base rotates camera from looking down −Z (USD default)
            # to looking horizontal along +Y.  self._pitch offsets from there.
            pitch_attr.Set(float(90.0 + self._pitch))
