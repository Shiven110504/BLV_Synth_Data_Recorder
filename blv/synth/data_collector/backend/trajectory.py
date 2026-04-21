"""Trajectory recording, playback, and directory management.

* :class:`TrajectoryRecorder` — subscribes to the per-frame update loop,
  samples ``GamepadCameraController.get_pose()`` every frame, and
  accumulates the sequence in memory.
* :class:`TrajectoryPlayer` — loads a trajectory, replays it frame by
  frame via the update loop, and fires ``on_complete`` once the last
  frame has been applied.
* :class:`TrajectoryManager` — wraps a directory of trajectory JSON
  files (list / load / save).

File I/O lives in :mod:`trajectory_io` so it remains unit-testable
without Isaac Sim on the PYTHONPATH.  This module owns only the
update-loop subscriptions and the runtime state that goes with them.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

import carb
import omni.kit.app

from . import trajectory_io as _io


# ===================================================================== #
#  TrajectoryRecorder                                                    #
# ===================================================================== #


class TrajectoryRecorder:
    """Records per-frame camera poses from a ``GamepadCameraController``."""

    def __init__(self, camera_controller) -> None:
        self._controller = camera_controller
        self._recording: bool = False
        self._frames: List[Dict[str, Any]] = []
        self._frame_count: int = 0
        self._update_sub = None
        self._name: str = ""
        self._environment: str = ""

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def start_recording(
        self,
        name: str = "trajectory",
        environment: str = "",
    ) -> None:
        if self._recording:
            carb.log_warn("[BLV] Recording already in progress — ignoring start.")
            return

        self._frames = []
        self._frame_count = 0
        self._name = name
        self._environment = environment
        self._recording = True

        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(
                self._on_update, name="blv.trajectory_recorder"
            )
        )
        carb.log_info(f"[BLV] Trajectory recording started — name='{name}'")

    def stop_recording(self) -> Dict[str, Any]:
        """Stop recording and return the trajectory data dict.

        Safe to call when not recording — returns an empty payload.
        """
        was_recording = self._recording
        self._recording = False

        # Explicit unsubscribe (see BUG 2 in refactor plan) — dropping the
        # reference alone doesn't always tear the Carb subscription down
        # immediately on reload.
        if self._update_sub is not None:
            try:
                self._update_sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._update_sub = None

        data = self._build_trajectory_data()
        if was_recording:
            carb.log_info(
                f"[BLV] Trajectory recording stopped — {self._frame_count} frames."
            )
        return data

    def save_trajectory(self, filepath: str) -> str:
        """Serialize the most-recently recorded trajectory to a JSON file."""
        data = self._build_trajectory_data()
        _io.write_trajectory_json(data, filepath)
        carb.log_info(f"[BLV] Trajectory saved → {filepath}")
        return filepath

    def _on_update(self, event) -> None:
        if not self._recording:
            return
        pose = self._controller.get_pose()
        self._frames.append({
            "frame": self._frame_count,
            "position": pose["position"],
            "rotation": pose["rotation"],
        })
        self._frame_count += 1

    def _build_trajectory_data(self) -> Dict[str, Any]:
        return _io.build_trajectory_payload(
            name=self._name,
            environment=self._environment,
            camera_path=self._controller.camera_path,
            frames=self._frames,
        )


# ===================================================================== #
#  TrajectoryPlayer                                                      #
# ===================================================================== #


class TrajectoryPlayer:
    """Replays a recorded trajectory frame-by-frame."""

    def __init__(self, camera_controller) -> None:
        self._controller = camera_controller
        self._trajectory: Optional[Dict[str, Any]] = None
        self._playing: bool = False
        self._current_frame: int = 0
        self._update_sub = None
        self._on_complete_callback: Optional[Callable[[], None]] = None

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def total_frames(self) -> int:
        if self._trajectory and "frames" in self._trajectory:
            return len(self._trajectory["frames"])
        return 0

    @property
    def trajectory_name(self) -> str:
        return self._trajectory.get("name", "") if self._trajectory else ""

    def load_trajectory(self, filepath: str) -> Dict[str, Any]:
        self._trajectory = _io.read_trajectory_json(filepath)
        carb.log_info(
            f"[BLV] Loaded trajectory '{self._trajectory.get('name', '?')}' "
            f"({self.total_frames} frames) from {filepath}"
        )
        return self._trajectory

    def load_trajectory_data(self, data: Dict[str, Any]) -> None:
        self._trajectory = data

    def play(self, on_complete: Optional[Callable[[], None]] = None) -> None:
        if self._trajectory is None or self.total_frames == 0:
            carb.log_warn("[BLV] No trajectory loaded — cannot play.")
            return
        if self._playing:
            carb.log_warn("[BLV] Playback already in progress.")
            return

        self._current_frame = 0
        self._playing = True
        self._on_complete_callback = on_complete

        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(
                self._on_update, name="blv.trajectory_player"
            )
        )
        carb.log_info(
            f"[BLV] Trajectory playback started — {self.total_frames} frames."
        )

    def stop(self, fire_on_complete: bool = False) -> None:
        """Stop playback.

        Parameters
        ----------
        fire_on_complete:
            When True, runs the ``on_complete`` callback even though
            playback was interrupted.  Used by the "Record with
            Trajectory" workflow to tear down the recorder regardless
            of how playback ended.
        """
        if not self._playing and self._update_sub is None:
            return

        self._playing = False
        if self._update_sub is not None:
            try:
                self._update_sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._update_sub = None
        carb.log_info("[BLV] Trajectory playback stopped.")

        if fire_on_complete and self._on_complete_callback is not None:
            cb, self._on_complete_callback = self._on_complete_callback, None
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                carb.log_error(f"[BLV] on_complete callback error: {exc}")

    def _on_update(self, event) -> None:
        if not self._playing or self._trajectory is None:
            return

        frames = self._trajectory["frames"]

        if self._current_frame >= len(frames):
            # End of trajectory — tear the subscription down explicitly.
            self._playing = False
            if self._update_sub is not None:
                try:
                    self._update_sub.unsubscribe()
                except Exception:  # noqa: BLE001
                    pass
                self._update_sub = None
            carb.log_info("[BLV] Trajectory playback complete.")
            if self._on_complete_callback:
                cb, self._on_complete_callback = self._on_complete_callback, None
                try:
                    cb()
                except Exception as exc:  # noqa: BLE001
                    carb.log_error(f"[BLV] on_complete callback error: {exc}")
            return

        frame_data = frames[self._current_frame]
        self._controller.set_pose(frame_data["position"], frame_data["rotation"])
        self._current_frame += 1


# ===================================================================== #
#  TrajectoryManager                                                     #
# ===================================================================== #


class TrajectoryManager:
    """Wraps a directory of trajectory JSON files.

    All real I/O goes through :mod:`trajectory_io`; this class only
    tracks the active directory and exposes a familiar facade.
    """

    TRAJECTORY_EXT: str = _io.TRAJECTORY_EXT

    def __init__(self, directory: str = "") -> None:
        self._directory: str = directory

    @property
    def directory(self) -> str:
        return self._directory

    @directory.setter
    def directory(self, path: str) -> None:
        self._directory = path

    def set_project_paths(
        self,
        root_folder: str,
        environment: str,
        class_name: str = "",
        location: str = "",
    ) -> str:
        expanded = os.path.normpath(os.path.expanduser(root_folder))
        parts = [expanded]
        if class_name:
            parts.append(class_name)
        parts.append(environment)
        if location:
            parts.append(location)
        parts.append("trajectories")
        self._directory = os.path.join(*parts)
        os.makedirs(self._directory, exist_ok=True)
        carb.log_info(f"[BLV] TrajectoryManager directory → {self._directory}")
        return self._directory

    def list_trajectories(self) -> List[str]:
        return _io.list_trajectory_files(self._directory)

    def list_trajectory_names(self) -> List[str]:
        return _io.list_trajectory_names(self._directory)

    def list_trajectory_info(self) -> List[Dict[str, Any]]:
        return _io.list_trajectory_info(self._directory)

    def load(self, filename: str) -> Dict[str, Any]:
        filepath = os.path.join(self._directory, filename)
        data = _io.read_trajectory_json(filepath)
        carb.log_info(f"[BLV] TrajectoryManager loaded {filepath}")
        return data

    def save(self, data: Dict[str, Any], filename: str) -> str:
        filepath = os.path.join(self._directory, filename)
        _io.write_trajectory_json(data, filepath)
        carb.log_info(f"[BLV] TrajectoryManager saved {filepath}")
        return filepath
