"""Session — top-level backend orchestrator shared by UI and CLI.

A :class:`Session` owns one instance of every backend module and
exposes coarse workflow methods (record a trajectory, capture along
it, iterate the full collect-all plan, …) that take a ``progress_cb``
for out-of-band UI updates.  The UI maps each button click onto one
method call; the CLI wires the same methods into argparse subcommands.

Invariants
----------
* The UI should contain no ``omni`` / ``pxr`` / ``carb`` / asyncio
  logic beyond what widget rendering requires.  Every workflow lives
  on :class:`Session`.
* Exactly one :class:`StageController` and one :class:`EventBus` per
  session.  Every backend module registered with the bus or the
  controller is torn down from :meth:`destroy`.
* Every ``_async`` method takes a ``progress_cb`` so the caller
  (UI or CLI) can render progress however it wants.  The callback
  signature is ``cb(fraction: float | None, status: str, detail:
  str = "")``.  ``fraction=None`` means "indeterminate" — use for
  status-only updates.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import carb
import omni.kit.app
import omni.usd
from pxr import Gf

from .asset_browser import AssetBrowser
from .capture import DEFAULT_ANNOTATORS, DataRecorder
from .config import Defaults, load_config
from .events import EventBus
from .gamepad_camera import GamepadCameraController
from .location import LocationManager
from . import paths as _paths
from .stage import StageController
from . import trajectory_io as _tio
from .trajectory import TrajectoryManager, TrajectoryPlayer, TrajectoryRecorder


# Number of render frames to wait after loading a new asset before
# capture begins.  Empirical default — lower and the first few frames
# can contain ghost residue from the previous prim.
_ASSET_WARMUP_FRAMES: int = 10

# Debounce threshold for auto-save of location transforms.  Counts
# bus events, not frames; 1 is fine because the bus only fires when
# the transform actually changed.  Higher values would be useful if
# we wanted to coalesce many small edits into one write.
_AUTOSAVE_DEBOUNCE_TICKS: int = 0


ProgressCb = Callable[..., None]


def _null_progress(*args: Any, **kwargs: Any) -> None:
    return None


class Session:
    """Owns every backend module for one simulation run.

    UI and CLI share the same Session class.  The UI instantiates it
    from its window constructor; the CLI instantiates it after booting
    a headless ``SimulationApp``.
    """

    def __init__(
        self,
        defaults: Optional[Defaults] = None,
    ) -> None:
        self._defaults: Defaults = defaults or load_config()

        # Project state (mutated by apply_project_settings)
        self._root_folder: str = self._defaults.root_folder
        self._environment: str = self._defaults.environment
        self._class_name: str = self._defaults.asset_class_name
        self._resolution: Tuple[int, int] = self._defaults.resolution
        self._rt_subframes: int = int(self._defaults.rt_subframes)
        self._annotators: Dict[str, bool] = dict(self._defaults.annotators)

        # Event bus — wired first so module ctors can subscribe.
        self.bus: EventBus = EventBus()

        # Backend modules.
        self.camera: GamepadCameraController = GamepadCameraController(
            camera_prim_path=self._defaults.camera_path,
            move_speed=self._defaults.move_speed,
            look_speed=self._defaults.look_speed,
            focal_length=self._defaults.focal_length,
        )
        self.recorder: DataRecorder = DataRecorder(
            camera_path=self._defaults.camera_path,
            resolution=self._resolution,
            annotators=self._annotators,
        )
        self.traj_recorder: TrajectoryRecorder = TrajectoryRecorder(self.camera)
        self.traj_player: TrajectoryPlayer = TrajectoryPlayer(self.camera)
        self.traj_manager: TrajectoryManager = TrajectoryManager()
        self.locations: LocationManager = LocationManager()
        self.assets: AssetBrowser = AssetBrowser(
            parent_prim_path=self._defaults.parent_prim_path,
            bus=self.bus,
        )

        # Stage controller + hooks.
        self.stage: StageController = StageController(self.bus)
        self._register_stage_hooks()

        # Auto-save plumbing.
        self.bus.subscribe(
            AssetBrowser.TRANSFORM_CHANGED_EVENT, self._on_asset_moved
        )
        self._gamepad_was_enabled_before_swap: bool = False

        # Run-folder cache — reset between workflows.
        self._default_run_name: Optional[str] = None

        # Trajectory name the user typed into the UI.  Read by the
        # gamepad X-button path so gamepad-started recordings get the
        # same filename as button-started ones.
        self._pending_traj_name: str = ""

        # The user binds this to toggle trajectory recording from the
        # gamepad X button.  The UI re-populates the name.
        self.camera.record_toggle_callback = self._on_gamepad_record_toggle

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def destroy(self) -> None:
        """Tear down every subsystem that holds an update subscription."""
        try:
            self.traj_player.stop(fire_on_complete=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.traj_recorder.is_recording:
                self.traj_recorder.stop_recording()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.camera.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.recorder.teardown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.assets.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.stage.destroy()
        except Exception:  # noqa: BLE001
            pass
        self.bus.clear()

    # ------------------------------------------------------------------ #
    #  Stage hook registration                                            #
    # ------------------------------------------------------------------ #

    def _register_stage_hooks(self) -> None:
        async def stop_traj_player() -> None:
            self.traj_player.stop(fire_on_complete=False)

        async def stop_traj_recorder() -> None:
            if self.traj_recorder.is_recording:
                self.traj_recorder.stop_recording()

        async def disable_gamepad() -> None:
            self._gamepad_was_enabled_before_swap = self.camera.is_enabled
            await self.camera.disable_async()

        async def clear_assets() -> None:
            self.assets.clear_stage_state()

        async def prep_recorder() -> None:
            if self.recorder.is_setup:
                await self.recorder.prepare_for_stage_change_async()

        for hook in (
            stop_traj_player,
            stop_traj_recorder,
            disable_gamepad,
            clear_assets,
            prep_recorder,
        ):
            self.stage.add_pre_close_hook(hook)

        async def ensure_camera_prim() -> None:
            # Ensure the BLV camera prim exists on the freshly opened
            # stage before the DataRecorder tries to create a render
            # product.  Without this, collect-all fails with "no valid
            # sensor paths" when the gamepad was never enabled.
            try:
                self.camera.ensure_camera_prim()
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(
                    f"[BLV] Could not ensure camera prim post-stage-swap: {exc}"
                )

        async def reenable_gamepad() -> None:
            if self._gamepad_was_enabled_before_swap:
                try:
                    self.camera.enable()
                except Exception as exc:  # noqa: BLE001
                    carb.log_warn(
                        f"[BLV] Could not re-enable gamepad post-stage-swap: {exc}"
                    )
                self._gamepad_was_enabled_before_swap = False

        self.stage.add_post_open_hook(ensure_camera_prim)
        self.stage.add_post_open_hook(reenable_gamepad)

    # ------------------------------------------------------------------ #
    #  Project settings                                                   #
    # ------------------------------------------------------------------ #

    def apply_project_settings(
        self,
        root_folder: str,
        environment: str,
        class_name: str,
        resolution: Optional[Tuple[int, int]] = None,
        rt_subframes: Optional[int] = None,
    ) -> None:
        """Apply (or re-apply) the project-level paths + render settings."""
        env_or_class_changed = (
            environment != self._environment
            or class_name != self._class_name
        )

        self._root_folder = root_folder
        self._environment = environment
        self._class_name = class_name
        if resolution is not None:
            self._resolution = tuple(resolution)
            self.recorder.resolution = self._resolution
        if rt_subframes is not None:
            self._rt_subframes = int(rt_subframes)
            self.recorder.rt_subframes = self._rt_subframes

        if class_name and environment:
            self.locations.set_base_directory(
                root_folder, class_name, environment
            )
            if env_or_class_changed:
                self.locations.current_location = ""
            self.traj_manager.set_project_paths(
                root_folder,
                environment,
                class_name=class_name,
                location=self.locations.current_location,
            )
        else:
            self.traj_manager.directory = ""
            self.locations.current_location = ""

        self._default_run_name = None

    def set_location(self, name: str) -> None:
        self.locations.current_location = name
        if self._class_name and self._environment:
            self.traj_manager.set_project_paths(
                self._root_folder,
                self._environment,
                class_name=self._class_name,
                location=name,
            )

    # ------------------------------------------------------------------ #
    #  Camera control                                                    #
    # ------------------------------------------------------------------ #

    def enable_gamepad(self) -> None:
        self.camera.enable()

    def disable_gamepad(self) -> None:
        self.camera.disable()

    def set_camera_path(self, path: str) -> None:
        self.camera.camera_path = path
        self.recorder.camera_path = path

    def set_move_speed(self, val: float) -> None:
        self.camera.move_speed = val

    def set_look_speed(self, val: float) -> None:
        self.camera.look_speed = val

    def set_focal_length(self, val: float) -> None:
        self.camera.focal_length = val

    # ------------------------------------------------------------------ #
    #  Trajectory recording / playback                                    #
    # ------------------------------------------------------------------ #

    def set_trajectory_name(self, name: str) -> None:
        """Stash the trajectory-name the user has typed into the UI.

        Used by the gamepad X-button path so hitting X produces the
        same filename the button would.
        """
        self._pending_traj_name = (name or "").strip()

    def start_trajectory_recording(self, name: str) -> None:
        self.traj_recorder.start_recording(name=name, environment=self._environment)

    def stop_trajectory_recording(self) -> Optional[str]:
        """Stop and save the in-progress recording.

        Returns the file path written to disk, or ``None`` if nothing
        was captured.  Emits ``"trajectory_saved"`` on the bus so the
        UI can refresh its playback / record-with-trajectory dropdowns.
        """
        if not self.traj_recorder.is_recording:
            return None
        data = self.traj_recorder.stop_recording()
        if data.get("frame_count", 0) == 0:
            return None
        filename = f"{data.get('name', 'trajectory')}.json"
        path = self.traj_manager.save(data, filename)
        frame_count = data.get("frame_count", 0)
        try:
            self.bus.emit("trajectory_saved", path, frame_count)
        except Exception as exc:  # noqa: BLE001
            carb.log_warn(f"[BLV] trajectory_saved emit failed: {exc}")
        return path

    def _on_gamepad_record_toggle(self) -> None:
        """Handle the X-button: toggle trajectory recording in-place."""
        if self.traj_recorder.is_recording:
            self.stop_trajectory_recording()
        else:
            # Prefer the name the user typed in the UI.  Fall back to
            # a timestamp only if nothing was set.
            name = self._pending_traj_name
            if not name:
                from datetime import datetime

                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                name = f"traj_{stamp}"
            self.start_trajectory_recording(name=name)

    def play_trajectory(
        self, filename: str, on_complete: Optional[Callable[[], None]] = None
    ) -> None:
        filepath = os.path.join(self.traj_manager.directory, filename)
        self.traj_player.load_trajectory(filepath)
        self.traj_player.play(on_complete=on_complete)

    def stop_trajectory_playback(self) -> None:
        self.traj_player.stop(fire_on_complete=False)

    # ------------------------------------------------------------------ #
    #  Location CRUD                                                     #
    # ------------------------------------------------------------------ #

    def create_location(self, name: str) -> str:
        """Create a location from the asset browser's current spawn transform."""
        t = self.assets.spawn_translate
        r = self.assets.spawn_orient
        s = self.assets.spawn_scale
        filepath = self.locations.create_location(
            name,
            translate=[t[0], t[1], t[2]],
            orient=[r.GetReal(), r.GetImaginary()[0], r.GetImaginary()[1], r.GetImaginary()[2]],
            scale=[s[0], s[1], s[2]],
        )
        return filepath

    def delete_location(self, name: str, confirmed: bool = False) -> bool:
        """Delete a location directory.

        The UI wires an inline confirm row; pass ``confirmed=True`` once
        the user has clicked confirm.  Raises :class:`ValueError` on
        unconfirmed calls to force the caller to go through the prompt.
        """
        if not confirmed:
            raise ValueError("delete_location requires explicit confirmation")
        return self.locations.delete_location(name)

    def save_current_transform(self) -> bool:
        """Persist the asset browser's current transform to the active location."""
        if not self.locations.has_location_selected:
            return False
        snap = self.assets.read_current_prim_transform()
        if snap is None:
            return False
        t, r, s = snap
        self.locations.save_transform(
            self.locations.current_location,
            translate=[t[0], t[1], t[2]],
            orient=[r.GetReal(), r.GetImaginary()[0], r.GetImaginary()[1], r.GetImaginary()[2]],
            scale=[s[0], s[1], s[2]],
        )
        return True

    def _on_asset_moved(
        self,
        translate: List[float],
        orient: List[float],
        scale: List[float],
    ) -> None:
        """Bus handler — debounced write-back of spawn transforms."""
        if not self.locations.has_location_selected:
            return
        try:
            self.locations.save_transform(
                self.locations.current_location,
                translate=translate,
                orient=orient,
                scale=scale,
            )
        except Exception as exc:  # noqa: BLE001
            carb.log_warn(f"[BLV] Auto-save location transform failed: {exc}")

    # ------------------------------------------------------------------ #
    #  Capture paths                                                     #
    # ------------------------------------------------------------------ #

    def _location_base_dir(self) -> str:
        return _paths.location_dir(
            self._root_folder,
            self._class_name,
            self._environment,
            self.locations.current_location,
        )

    def _current_run_name(self) -> str:
        stem = self.assets.current_asset_stem
        if stem:
            return _paths.sanitize_folder_name(stem)
        if self._default_run_name:
            return self._default_run_name
        self._default_run_name = _paths.next_default_run_name(
            self._location_base_dir()
        )
        return self._default_run_name

    def capture_output_dir(self, traj_stem: str = "") -> str:
        base = self._location_base_dir()
        return _paths.run_dir(base, self._current_run_name(), traj_stem)

    # ------------------------------------------------------------------ #
    #  Capture workflows                                                 #
    # ------------------------------------------------------------------ #

    async def record_with_trajectory(
        self,
        trajectory_filename: str,
        frame_step: int = 1,
        progress_cb: ProgressCb = _null_progress,
    ) -> int:
        """Replay one trajectory against the current asset and capture.

        Returns the number of frames captured.  Cancel by cancelling
        the task.
        """
        if not self._class_name:
            raise ValueError("Class Name is required — apply project settings first")

        # Discard any render product left over from a previous run.
        # Between workflow invocations, idle Kit ticks can garbage-collect
        # the OmniGraph nodes backing the product, leaving a zombified
        # handle that causes "accessed invalid null prim" on the next
        # step_async.  ensure_setup() below will create a fresh one.
        self.recorder.teardown()

        # Make the capture path self-sufficient — don't require the user
        # to have enabled the gamepad or gone through StageController.
        self.camera.ensure_camera_prim()

        traj_stem = os.path.splitext(trajectory_filename)[0]
        output_dir = self.capture_output_dir(traj_stem)
        traj_path = os.path.join(self.traj_manager.directory, trajectory_filename)

        progress_cb(None, "Loading trajectory", trajectory_filename)
        try:
            trajectory = _tio.read_trajectory_json(traj_path)
        except Exception as exc:  # noqa: BLE001
            progress_cb(None, f"Error loading trajectory: {exc}", "")
            raise

        frames = trajectory.get("frames", [])
        if not frames:
            progress_cb(1.0, "Trajectory has 0 frames — nothing to capture", "")
            return 0

        frame_step = max(1, int(frame_step))
        sampled = list(range(0, len(frames), frame_step))

        progress_cb(None, "Setting up writer", output_dir)
        self.recorder.ensure_setup(
            output_dir=output_dir,
            resolution=self._resolution,
            rt_subframes=self._rt_subframes,
            camera_path=self.camera.camera_path,
            annotators=self._annotators,
        )

        captured = 0
        for i, frame_idx in enumerate(sampled):
            frame_data = frames[frame_idx]
            self.camera.set_pose(frame_data["position"], frame_data["rotation"])
            await omni.kit.app.get_app().next_update_async()
            await self.recorder.capture_frame()
            captured += 1
            progress_cb(
                (i + 1) / len(sampled),
                f"Capturing {i + 1}/{len(sampled)}",
                f"frame {frame_idx}/{len(frames)}",
            )

        return captured

    async def record_all_trajectories(
        self,
        frame_step: int = 1,
        progress_cb: ProgressCb = _null_progress,
    ) -> int:
        """Replay every trajectory at the current location for every asset.

        Assumes the asset browser is already scanned and the current
        location is set.  Returns total frames captured.
        """
        if not self._class_name:
            raise ValueError("Class Name is required — apply project settings first")
        if not self.locations.has_location_selected:
            raise ValueError("No location selected")

        # Discard any render product left over from a previous run.
        self.recorder.teardown()

        # Make the capture path self-sufficient — don't require the user
        # to have enabled the gamepad or gone through StageController.
        self.camera.ensure_camera_prim()

        traj_names = self.traj_manager.list_trajectory_names()
        if not traj_names:
            progress_cb(1.0, "No trajectories at this location", "")
            return 0
        total_assets = self.assets.total_assets
        if total_assets < 1:
            raise ValueError("No assets scanned — set the asset folder first")

        total_units = total_assets * len(traj_names)
        completed_units = 0
        overall_captured = 0

        for asset_idx in range(total_assets):
            self.assets.load_asset(asset_idx, preserve_transform=False)
            stem = self.assets.current_asset_stem

            progress_cb(
                completed_units / total_units,
                f"Asset {asset_idx + 1}/{total_assets}: {stem}",
                "warming up",
            )
            for _ in range(_ASSET_WARMUP_FRAMES):
                await omni.kit.app.get_app().next_update_async()

            for traj_idx, traj_name in enumerate(traj_names):
                traj_stem = os.path.splitext(traj_name)[0]
                output_dir = self.capture_output_dir(traj_stem)
                traj_path = os.path.join(self.traj_manager.directory, traj_name)

                try:
                    trajectory = _tio.read_trajectory_json(traj_path)
                except Exception as exc:  # noqa: BLE001
                    carb.log_error(f"[BLV] record_all: {traj_path}: {exc}")
                    completed_units += 1
                    continue

                frames = trajectory.get("frames", [])
                if not frames:
                    completed_units += 1
                    continue

                self.recorder.ensure_setup(
                    output_dir=output_dir,
                    resolution=self._resolution,
                    rt_subframes=self._rt_subframes,
                    camera_path=self.camera.camera_path,
                    annotators=self._annotators,
                )

                sampled = list(range(0, len(frames), max(1, frame_step)))
                for i, frame_idx in enumerate(sampled):
                    fd = frames[frame_idx]
                    self.camera.set_pose(fd["position"], fd["rotation"])
                    await omni.kit.app.get_app().next_update_async()
                    await self.recorder.capture_frame()
                    frac = (
                        completed_units + (i + 1) / len(sampled)
                    ) / total_units
                    progress_cb(
                        frac,
                        f"Asset {asset_idx + 1}/{total_assets}: {stem} | "
                        f"Traj {traj_idx + 1}/{len(traj_names)}: {traj_stem}",
                        f"frame {i + 1}/{len(sampled)}",
                    )
                overall_captured += len(sampled)
                completed_units += 1

        return overall_captured

    async def collect_all(
        self,
        envs_folder: str,
        frame_step: int = 1,
        on_env_error: str = "skip",
        progress_cb: ProgressCb = _null_progress,
    ) -> int:
        """Iterate envs × locations × assets × trajectories.

        Uses :meth:`StageController.switch_to` for every environment
        change — no bespoke teardown here.
        """
        if not self._class_name:
            raise ValueError("Class Name is required — apply project settings first")

        # Discard any render product left over from a previous run.
        # The first switch_to() would also tear down via its pre-close
        # hook, but doing it here avoids C++ errors from trying to drain
        # a stale orchestrator inside that hook.
        self.recorder.teardown()

        plans = _paths.plan_collect_all(
            self._root_folder, self._class_name, envs_folder
        )
        if not plans:
            progress_cb(1.0, "No environments found with trajectories", "")
            return 0

        total_assets = self.assets.total_assets
        if total_assets < 1:
            raise ValueError("No assets scanned — set the asset folder first")

        total_units = sum(
            total_assets * len(trajs)
            for p in plans
            for trajs in p.locations.values()
        )
        completed_units = 0
        overall_captured = 0

        original_env = self._environment
        original_location = self.locations.current_location

        try:
            for env_idx, plan in enumerate(plans):
                progress_cb(
                    completed_units / total_units if total_units else None,
                    f"Env {env_idx + 1}/{len(plans)}: {plan.env_name}",
                    "loading scene",
                )

                ok = await self.stage.switch_to(plan.usd_path)
                if not ok:
                    msg = f"failed to load {plan.usd_path}"
                    carb.log_error(f"[BLV] collect_all: {msg}")
                    if on_env_error == "abort":
                        raise RuntimeError(msg)
                    # Still advance the counter so the progress bar stays honest.
                    for trajs in plan.locations.values():
                        completed_units += total_assets * len(trajs)
                    continue

                self._environment = plan.env_name
                self.locations.set_base_directory(
                    self._root_folder, self._class_name, plan.env_name
                )

                for loc_idx, (loc_name, traj_names) in enumerate(
                    plan.locations.items()
                ):
                    self.set_location(loc_name)

                    # Load this location's spawn transform into the asset
                    # browser so every asset we drop here starts at the
                    # same pose.
                    try:
                        loc = self.locations.load_location(loc_name)
                        t = loc["spawn_transform"]["translate"]
                        r = loc["spawn_transform"]["orient"]
                        s = loc["spawn_transform"]["scale"]
                        self.assets.set_spawn_transform(
                            Gf.Vec3d(*t),
                            Gf.Quatd(r[0], r[1], r[2], r[3]),
                            Gf.Vec3d(*s),
                        )
                    except Exception as exc:  # noqa: BLE001
                        carb.log_warn(
                            f"[BLV] collect_all: could not load "
                            f"{loc_name}/location.json: {exc}"
                        )

                    for asset_idx in range(total_assets):
                        self.assets.load_asset(
                            asset_idx, preserve_transform=False
                        )
                        stem = self.assets.current_asset_stem
                        for _ in range(_ASSET_WARMUP_FRAMES):
                            await omni.kit.app.get_app().next_update_async()

                        for traj_idx, traj_name in enumerate(traj_names):
                            traj_stem = os.path.splitext(traj_name)[0]
                            output_dir = self.capture_output_dir(traj_stem)
                            traj_path = os.path.join(
                                self.traj_manager.directory, traj_name
                            )

                            try:
                                trajectory = _tio.read_trajectory_json(traj_path)
                            except Exception as exc:  # noqa: BLE001
                                carb.log_error(
                                    f"[BLV] collect_all: {traj_path}: {exc}"
                                )
                                completed_units += 1
                                continue

                            frames = trajectory.get("frames", [])
                            if not frames:
                                completed_units += 1
                                continue

                            self.recorder.ensure_setup(
                                output_dir=output_dir,
                                resolution=self._resolution,
                                rt_subframes=self._rt_subframes,
                                camera_path=self.camera.camera_path,
                                annotators=self._annotators,
                            )

                            sampled = list(
                                range(0, len(frames), max(1, frame_step))
                            )
                            for i, frame_idx in enumerate(sampled):
                                fd = frames[frame_idx]
                                self.camera.set_pose(
                                    fd["position"], fd["rotation"]
                                )
                                await omni.kit.app.get_app().next_update_async()
                                await self.recorder.capture_frame()
                                frac = (
                                    completed_units
                                    + (i + 1) / len(sampled)
                                ) / total_units
                                progress_cb(
                                    frac,
                                    f"Env {env_idx + 1}/{len(plans)}: "
                                    f"{plan.env_name} | "
                                    f"Loc {loc_idx + 1}/{len(plan.locations)}: "
                                    f"{loc_name} | "
                                    f"Asset {asset_idx + 1}/{total_assets}: "
                                    f"{stem} | "
                                    f"Traj {traj_idx + 1}/{len(traj_names)}: "
                                    f"{traj_stem}",
                                    f"frame {i + 1}/{len(sampled)}",
                                )
                            overall_captured += len(sampled)
                            completed_units += 1
        except asyncio.CancelledError:
            progress_cb(None, "Cancelled", "")
            raise
        finally:
            # Restore every path-bearing module to the user's original
            # state — collect-all mutates _environment, LocationManager
            # _base_dir, and TrajectoryManager directory during the loop.
            self._environment = original_env
            if self._class_name and original_env:
                self.locations.set_base_directory(
                    self._root_folder, self._class_name, original_env
                )
            if original_location:
                self.set_location(original_location)

        progress_cb(
            1.0,
            f"Done — {overall_captured} frames across {len(plans)} environments",
            "",
        )
        return overall_captured
