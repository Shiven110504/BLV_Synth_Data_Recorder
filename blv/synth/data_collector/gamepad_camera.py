"""
GamepadCameraController v2 — FPS-style camera control via XInput gamepad.
=========================================================================

Designed for the **Logitech F710** in XInput mode (behaves identically to an
Xbox controller).  Left stick moves the camera (forward / back / strafe), right
stick controls look direction (yaw / pitch), triggers control vertical
movement, D-pad adjusts speed, and left bumper toggles a "slow" precision mode.

v2 fixes (from Shiven's testing)
---------------------------------
1. **raw_inputs dict pattern** — replaced the buggy numpy ``_axes`` array with a
   ``Dict[int, float]`` called ``_raw_inputs``.  Each ``GamepadInput`` enum
   member is stored as a separate key with its raw ``[0, 1]`` value.  Signed
   axes are computed in ``_on_update`` by subtracting opposite directions.
2. **Control mapping** — Left stick up/down is now FORWARD/BACKWARD (was
   incorrectly mapped to elevation).  Triggers control vertical movement.
3. **Pitch inversion fix** — ``self._pitch += pitch_ax`` (was ``-=``, which
   inverted the look direction).
4. **No pitch clamp** — Removed the 89° clamp that felt artificial and didn't
   match Isaac Sim's default gamepad behaviour.
5. **Speed defaults** — move_speed 5.0 (was 2.0), look_speed 45.0 (was 90.0),
   speed_step 1.0 (was 0.5).

Key implementation notes
------------------------
* Kit's built-in gamepad camera control (``/persistent/app/omniverse/
  gamepadCameraControl``) **must** be disabled before subscribing — otherwise
  two systems fight for the camera and input events are consumed by Kit.
* ``GamepadInput`` axes (e.g. ``LEFT_STICK_UP``, ``LEFT_STICK_DOWN``) fire as
  **separate events** with absolute values in ``[0, 1]``.  They are NOT a
  single axis from ``-1`` to ``+1``.  We store each direction separately in
  ``_raw_inputs`` and compute signed axes in ``_on_update``.
* Subscription handles (``_gp_sub``, ``_update_sub``) are kept as instance
  attributes to prevent garbage collection, which would silently drop the
  callbacks.
* The camera prim uses ``xformOp:translate`` (``Gf.Vec3d``) +
  ``xformOp:rotateYXZ`` (``Gf.Vec3f`` — pitch, yaw, roll).  USD Camera
  convention is Y-up, negative-Z forward.
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
    """FPS-style camera controller driven by an XInput gamepad.

    Parameters
    ----------
    camera_prim_path : str
        USD path where the camera prim will be created / read.
    move_speed : float
        Base translation speed in metres per second.
    look_speed : float
        Base rotation speed in degrees per second.
    """

    # ------------------------------------------------------------------ #
    #  Constants — v2 tuned defaults                                      #
    # ------------------------------------------------------------------ #
    DEFAULT_MOVE_SPEED: float = 5.0      # metres / second (was 2.0)
    DEFAULT_LOOK_SPEED: float = 45.0     # degrees / second (was 90.0)
    DEAD_ZONE: float = 0.15             # Logitech F710 sticks need generous dead-zone
    SPEED_STEP: float = 1.0             # Speed delta per D-pad press (was 0.5)
    SLOW_FACTOR: float = 0.25           # Multiplier when slow-mode is active
    MIN_MOVE_SPEED: float = 0.1         # Floor for movement speed
    MIN_LOOK_SPEED: float = 1.0         # Floor for look speed

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        camera_prim_path: str = "/World/BLV_Camera",
        move_speed: Optional[float] = None,
        look_speed: Optional[float] = None,
    ) -> None:
        # Read extension-level settings (fall back to class defaults)
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
        self._slow_mode: bool = False

        # Camera state (Euler angles in degrees, Y-up coordinate system)
        self._yaw: float = 0.0
        self._pitch: float = 0.0
        self._position: Gf.Vec3d = Gf.Vec3d(0.0, 0.0, 0.0)

        # v2 FIX 1: Raw inputs dict — each GamepadInput enum member is a
        # separate key storing its raw [0, 1] value.  Signed axes are computed
        # in _on_update by subtracting opposite directions.  This avoids the
        # accumulation bugs in the old numpy _axes array.
        self._raw_inputs: Dict[int, float] = {}

        # Carb input handles — keep alive to avoid GC dropping the callbacks
        self._input: carb.input.IInput = carb.input.acquire_input_interface()
        self._gamepad = omni.appwindow.get_default_app_window().get_gamepad(0)
        self._gp_sub = None          # gamepad event subscription
        self._update_sub = None      # per-frame update subscription

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def enable(self) -> None:
        """Activate gamepad camera control.

        * Disables Kit's built-in gamepad camera so we own the input.
        * Ensures the camera prim exists and sets the viewport to it.
        * Subscribes to gamepad events and the per-frame update loop.
        """
        if self._enabled:
            carb.log_warn("[BLV] GamepadCameraController already enabled.")
            return

        # --- Disable Kit's own gamepad camera ---
        carb.settings.get_settings().set_bool(
            "/persistent/app/omniverse/gamepadCameraControl", False
        )
        carb.log_info("[BLV] Disabled Kit built-in gamepad camera control.")

        # --- Camera prim ---
        self._ensure_camera_prim()
        self._read_camera_pose()

        # --- Clear any stale input state ---
        self._raw_inputs.clear()

        # --- Subscriptions ---
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
        """Deactivate gamepad camera control and restore Kit defaults."""
        if not self._enabled:
            return

        # Unsubscribe gamepad events
        if self._gp_sub is not None:
            self._input.unsubscribe_to_gamepad_events(self._gamepad, self._gp_sub)
            self._gp_sub = None

        # Drop update subscription (setting to None releases it)
        self._update_sub = None

        self._enabled = False

        # Re-enable Kit's built-in gamepad camera so other tools work
        carb.settings.get_settings().set_bool(
            "/persistent/app/omniverse/gamepadCameraControl", True
        )
        carb.log_info("[BLV] GamepadCameraController disabled.")

    def destroy(self) -> None:
        """Full teardown — call from extension shutdown."""
        self.disable()

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def is_enabled(self) -> bool:
        """Whether the controller is currently active."""
        return self._enabled

    @property
    def camera_path(self) -> str:
        """USD prim path of the controlled camera."""
        return self._camera_path

    @camera_path.setter
    def camera_path(self, path: str) -> None:
        self._camera_path = path
        if self._enabled:
            self._ensure_camera_prim()
            self._read_camera_pose()

    @property
    def move_speed(self) -> float:
        """Current base translation speed (m/s)."""
        return self._move_speed

    @move_speed.setter
    def move_speed(self, val: float) -> None:
        self._move_speed = max(self.MIN_MOVE_SPEED, val)

    @property
    def look_speed(self) -> float:
        """Current base rotation speed (deg/s)."""
        return self._look_speed

    @look_speed.setter
    def look_speed(self, val: float) -> None:
        self._look_speed = max(self.MIN_LOOK_SPEED, val)

    @property
    def slow_mode(self) -> bool:
        """Whether the slow-mode (fine adjustment) toggle is active."""
        return self._slow_mode

    # ------------------------------------------------------------------ #
    #  Pose helpers (used by trajectory recorder / player)                #
    # ------------------------------------------------------------------ #

    def get_pose(self) -> Dict[str, List[float]]:
        """Return the current camera pose as a serialisable dict.

        Returns
        -------
        dict
            ``{"position": [x, y, z], "rotation": [pitch, yaw, roll]}``
        """
        return {
            "position": [self._position[0], self._position[1], self._position[2]],
            "rotation": [self._pitch, self._yaw, 0.0],
        }

    def set_pose(self, position: List[float], rotation: List[float]) -> None:
        """Set the camera pose directly (bypasses gamepad input).

        Parameters
        ----------
        position : list[float]
            ``[x, y, z]`` in stage metres.
        rotation : list[float]
            ``[pitch, yaw, roll]`` in degrees.
        """
        self._position = Gf.Vec3d(*position)
        self._pitch = rotation[0]
        self._yaw = rotation[1]
        self._apply_pose_to_usd()

    # ------------------------------------------------------------------ #
    #  Internal — Camera prim management                                  #
    # ------------------------------------------------------------------ #

    def _ensure_camera_prim(self) -> None:
        """Create the camera prim if it does not already exist and point the
        active viewport at it."""
        stage: Usd.Stage = omni.usd.get_context().get_stage()
        if stage is None:
            carb.log_error("[BLV] No USD stage available — cannot create camera.")
            return

        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            cam = UsdGeom.Camera.Define(stage, self._camera_path)
            xformable = UsdGeom.Xformable(cam.GetPrim())
            xformable.ClearXformOpOrder()
            xformable.AddTranslateOp()     # xformOp:translate  → Gf.Vec3d
            xformable.AddRotateYXZOp()     # xformOp:rotateYXZ  → Gf.Vec3f
            carb.log_info(f"[BLV] Created camera prim at {self._camera_path}")

        # Point the active viewport at our camera
        try:
            from omni.kit.viewport.utility import get_active_viewport

            viewport = get_active_viewport()
            if viewport is not None:
                viewport.camera_path = self._camera_path
                carb.log_info(
                    f"[BLV] Viewport camera set to {self._camera_path}"
                )
        except Exception as exc:  # pragma: no cover
            carb.log_warn(f"[BLV] Could not set viewport camera: {exc}")

    def _read_camera_pose(self) -> None:
        """Read the existing camera transform from USD and sync internal state.

        This is called once at enable-time so that the controller starts from
        wherever the camera already is, rather than snapping to the origin.
        """
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        prim = stage.GetPrimAtPath(self._camera_path)
        if not prim.IsValid():
            return

        xformable = UsdGeom.Xformable(prim)
        world_mat = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        self._position = Gf.Vec3d(world_mat.ExtractTranslation())

        # Decompose rotation into yaw (Y), pitch (X), roll (Z).
        # Gf.Rotation.Decompose takes three axis vectors and returns the
        # angles in the order they were given.
        rotation = world_mat.ExtractRotation()
        decomp = rotation.Decompose(
            Gf.Vec3d(0, 1, 0),  # yaw axis
            Gf.Vec3d(1, 0, 0),  # pitch axis
            Gf.Vec3d(0, 0, 1),  # roll axis
        )
        self._yaw = decomp[0]
        self._pitch = decomp[1]

    # ------------------------------------------------------------------ #
    #  Internal — Gamepad event handler                                   #
    # ------------------------------------------------------------------ #

    def _on_gamepad_event(self, event, *args) -> bool:
        """Process a single gamepad event.

        v2 FIX 1: Uses raw_inputs dict pattern instead of a numpy array.
        ``GamepadInput`` axes emit separate events with absolute values in
        ``[0, 1]``.  We store each input enum as a key and compute signed
        axes later in ``_on_update``.
        """
        val: float = event.value
        inp = event.input
        G = carb.input.GamepadInput

        # Apply dead-zone — below threshold we treat as zero
        if abs(val) < self.DEAD_ZONE:
            val = 0.0

        # ---- Analog axis storage (v2: raw_inputs dict) ----
        # Store raw [0, 1] values for each direction separately.
        # Signed computation happens in _on_update.
        _analog = {
            G.LEFT_STICK_RIGHT, G.LEFT_STICK_LEFT,
            G.LEFT_STICK_UP, G.LEFT_STICK_DOWN,
            G.RIGHT_STICK_RIGHT, G.RIGHT_STICK_LEFT,
            G.RIGHT_STICK_UP, G.RIGHT_STICK_DOWN,
            G.LEFT_TRIGGER, G.RIGHT_TRIGGER,
        }
        if inp in _analog:
            self._raw_inputs[inp] = val

        # ---- Button events (digital, fire once on press) ----
        if inp == G.DPAD_UP and val > 0.5:
            self._move_speed += self.SPEED_STEP
            carb.log_info(f"[BLV] Move speed → {self._move_speed:.1f} m/s")

        elif inp == G.DPAD_DOWN and val > 0.5:
            self._move_speed = max(self.MIN_MOVE_SPEED, self._move_speed - self.SPEED_STEP)
            carb.log_info(f"[BLV] Move speed → {self._move_speed:.1f} m/s")

        elif inp == G.LEFT_SHOULDER and val > 0.5:
            self._slow_mode = not self._slow_mode
            carb.log_info(f"[BLV] Slow mode {'ON' if self._slow_mode else 'OFF'}")

        return True  # event consumed

    # ------------------------------------------------------------------ #
    #  Internal — Per-frame update                                        #
    # ------------------------------------------------------------------ #

    def _on_update(self, event) -> None:
        """Called every frame — applies accumulated axis state to the camera.

        v2 FIXES applied here:
        - FIX 1: Compute signed axes from raw_inputs dict
        - FIX 2: Correct control mapping (left stick = move, triggers = vertical)
        - FIX 3: Pitch sign corrected (+=, not -=)
        - FIX 4: No pitch clamp
        """
        if not self._enabled:
            return

        # Delta time — fall back to 60 fps if the payload is unavailable
        try:
            dt: float = event.payload["dt"]
        except Exception:
            dt = 1.0 / 60.0

        speed = self._move_speed * (self.SLOW_FACTOR if self._slow_mode else 1.0)

        # ---- Compute signed axes from raw inputs (v2 FIX 1) ----
        G = carb.input.GamepadInput
        ri = self._raw_inputs

        # Left stick: movement
        strafe = ri.get(G.LEFT_STICK_RIGHT, 0.0) - ri.get(G.LEFT_STICK_LEFT, 0.0)
        fwd = ri.get(G.LEFT_STICK_UP, 0.0) - ri.get(G.LEFT_STICK_DOWN, 0.0)

        # Right stick: look
        yaw_ax = ri.get(G.RIGHT_STICK_RIGHT, 0.0) - ri.get(G.RIGHT_STICK_LEFT, 0.0)
        pitch_ax = ri.get(G.RIGHT_STICK_UP, 0.0) - ri.get(G.RIGHT_STICK_DOWN, 0.0)

        # Triggers: vertical movement
        ltrig = ri.get(G.LEFT_TRIGGER, 0.0)
        rtrig = ri.get(G.RIGHT_TRIGGER, 0.0)

        # ---- Look (yaw / pitch) ----
        # Yaw: negative so stick-right rotates view right (decreases yaw angle)
        self._yaw -= yaw_ax * self._look_speed * dt
        # v2 FIX 3: Pitch sign corrected — stick up (positive pitch_ax) should
        # INCREASE pitch (look up).  Was incorrectly -= which inverted it.
        self._pitch += pitch_ax * self._look_speed * dt
        # v2 FIX 4: No pitch clamp — removed the 89° clamp that felt artificial
        # and didn't match Isaac Sim's default gamepad behaviour.

        # ---- Movement vectors ----
        yaw_rad = math.radians(self._yaw)

        # Forward vector projected onto the XZ ground plane (Y-up, -Z forward)
        forward = Gf.Vec3d(
            math.sin(yaw_rad),
            0.0,
            -math.cos(yaw_rad),
        )
        right = Gf.Vec3d(math.cos(yaw_rad), 0.0, math.sin(yaw_rad))
        up = Gf.Vec3d(0.0, 1.0, 0.0)

        # ---- Accumulate movement (v2 FIX 2: correct mapping) ----
        move = Gf.Vec3d(0.0, 0.0, 0.0)
        move += forward * (fwd * speed * dt)       # left stick forward/back
        move += right * (strafe * speed * dt)      # left stick strafe
        move += up * (rtrig * speed * dt)          # right trigger = UP
        move -= up * (ltrig * speed * dt)          # left trigger = DOWN

        self._position += move

        # ---- Write to USD ----
        self._apply_pose_to_usd()

    def _apply_pose_to_usd(self) -> None:
        """Write ``_position`` and ``_pitch/_yaw`` to the camera prim's
        xformOps.  Separated from ``_on_update`` so ``set_pose()`` can call it
        too.
        """
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
            # rotateYXZ order: (pitch around X, yaw around Y, roll around Z)
            rotate_attr.Set(Gf.Vec3f(self._pitch, self._yaw, 0.0))
