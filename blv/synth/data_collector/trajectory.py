"""
Trajectory recording, playback, and file management.
=====================================================

* **TrajectoryRecorder** — subscribes to the per-frame update loop, samples
  ``GamepadCameraController.get_pose()`` every frame, and accumulates the
  sequence in memory.  On stop the data is returned as a dict (and optionally
  saved to a JSON file).

* **TrajectoryPlayer** — loads a trajectory JSON, then replays it frame by
  frame via the update loop, calling ``GamepadCameraController.set_pose()``
  each tick.  When playback finishes it fires an ``on_complete`` callback so
  that the data-capture pipeline can chain the next step.

* **TrajectoryManager** — thin helper that wraps a directory of trajectory
  JSON files, providing listing / loading / saving utilities.  In v2 this
  integrates with the project-root directory structure.

JSON format
-----------
::

    {
        "version": "1.0",
        "name": "trajectory_001",
        "environment": "hospital_hallway",
        "camera_path": "/World/BLV_Camera",
        "fps": 60,
        "frame_count": 300,
        "created": "2026-04-06T13:00:00",
        "frames": [
            {"frame": 0, "position": [x, y, z], "rotation": [pitch, yaw, roll]},
            ...
        ]
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import carb
import omni.kit.app


# ===================================================================== #
#  TrajectoryRecorder                                                    #
# ===================================================================== #


class TrajectoryRecorder:
    """Records per-frame camera poses from a ``GamepadCameraController``.

    Parameters
    ----------
    camera_controller
        An instance of :class:`GamepadCameraController` whose ``get_pose()``
        will be sampled every frame while recording.
    """

    def __init__(self, camera_controller) -> None:
        self._controller = camera_controller
        self._recording: bool = False
        self._frames: List[Dict[str, Any]] = []
        self._frame_count: int = 0
        self._update_sub = None  # keep alive!
        self._name: str = ""
        self._environment: str = ""

    # ---- Properties -------------------------------------------------- #

    @property
    def is_recording(self) -> bool:
        """``True`` while a recording session is active."""
        return self._recording

    @property
    def frame_count(self) -> int:
        """Number of frames captured so far in the current session."""
        return self._frame_count

    # ---- Public API -------------------------------------------------- #

    def start_recording(
        self,
        name: str = "trajectory",
        environment: str = "",
    ) -> None:
        """Begin a new recording session.

        Parameters
        ----------
        name : str
            Human-readable name stored in the trajectory metadata.
        environment : str
            Free-form string describing the environment / scene being recorded.
        """
        if self._recording:
            carb.log_warn("[BLV] Recording already in progress — ignoring start.")
            return

        self._frames = []
        self._frame_count = 0
        self._name = name
        self._environment = environment
        self._recording = True

        # Subscribe to the update loop so we sample every rendered frame
        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update, name="blv.trajectory_recorder")
        )
        carb.log_info(f"[BLV] Trajectory recording started — name='{name}'")

    def stop_recording(self) -> Dict[str, Any]:
        """Stop recording and return the trajectory data dict.

        Returns
        -------
        dict
            Complete trajectory data including metadata and all frames.
        """
        self._recording = False
        self._update_sub = None  # release subscription
        data = self._build_trajectory_data()
        carb.log_info(
            f"[BLV] Trajectory recording stopped — {self._frame_count} frames."
        )
        return data

    def save_trajectory(self, filepath: str) -> str:
        """Save the most-recently recorded trajectory to a JSON file.

        Parameters
        ----------
        filepath : str
            Absolute path for the output ``.json`` file.  Parent directories
            are created automatically.

        Returns
        -------
        str
            The filepath that was written.
        """
        data = self._build_trajectory_data()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as fh:
            json.dump(data, fh, indent=2)
        carb.log_info(f"[BLV] Trajectory saved → {filepath}")
        return filepath

    # ---- Internal ---------------------------------------------------- #

    def _on_update(self, event) -> None:
        """Sample one pose per frame."""
        if not self._recording:
            return
        pose = self._controller.get_pose()
        self._frames.append(
            {
                "frame": self._frame_count,
                "position": pose["position"],
                "rotation": pose["rotation"],
            }
        )
        self._frame_count += 1

    def _build_trajectory_data(self) -> Dict[str, Any]:
        """Assemble the full trajectory payload."""
        return {
            "version": "1.0",
            "name": self._name,
            "environment": self._environment,
            "camera_path": self._controller.camera_path,
            "fps": 60,
            "frame_count": len(self._frames),
            "created": datetime.now().isoformat(),
            "frames": list(self._frames),  # shallow copy
        }


# ===================================================================== #
#  TrajectoryPlayer                                                      #
# ===================================================================== #


class TrajectoryPlayer:
    """Replays a recorded trajectory frame-by-frame.

    After loading trajectory data (from a file or dict) call :meth:`play` to
    begin playback.  Each update tick advances one frame.  When the last frame
    is reached the optional ``on_complete`` callback fires — this is the hook
    used by the "Record with Trajectory" workflow to know when to stop the data
    capture pipeline.

    Parameters
    ----------
    camera_controller
        An instance of :class:`GamepadCameraController` whose ``set_pose()``
        will be called each frame during playback.
    """

    def __init__(self, camera_controller) -> None:
        self._controller = camera_controller
        self._trajectory: Optional[Dict[str, Any]] = None
        self._playing: bool = False
        self._current_frame: int = 0
        self._update_sub = None
        self._on_complete_callback: Optional[Callable[[], None]] = None

    # ---- Properties -------------------------------------------------- #

    @property
    def is_playing(self) -> bool:
        """``True`` while playback is active."""
        return self._playing

    @property
    def current_frame(self) -> int:
        """Index of the frame that will be played next."""
        return self._current_frame

    @property
    def total_frames(self) -> int:
        """Total number of frames in the loaded trajectory (0 if none loaded)."""
        if self._trajectory and "frames" in self._trajectory:
            return len(self._trajectory["frames"])
        return 0

    @property
    def trajectory_name(self) -> str:
        """Name field of the loaded trajectory, or empty string."""
        if self._trajectory:
            return self._trajectory.get("name", "")
        return ""

    # ---- Public API -------------------------------------------------- #

    def load_trajectory(self, filepath: str) -> Dict[str, Any]:
        """Load a trajectory from a JSON file on disk.

        Parameters
        ----------
        filepath : str
            Path to the ``.json`` trajectory file.

        Returns
        -------
        dict
            The parsed trajectory data.

        Raises
        ------
        FileNotFoundError
            If *filepath* does not exist.
        json.JSONDecodeError
            If the file is not valid JSON.
        """
        with open(filepath, "r") as fh:
            self._trajectory = json.load(fh)
        carb.log_info(
            f"[BLV] Loaded trajectory '{self._trajectory.get('name', '?')}' "
            f"({self.total_frames} frames) from {filepath}"
        )
        return self._trajectory

    def load_trajectory_data(self, data: Dict[str, Any]) -> None:
        """Load a trajectory from an in-memory dict (e.g. returned by
        ``TrajectoryRecorder.stop_recording()``)."""
        self._trajectory = data

    def play(self, on_complete: Optional[Callable[[], None]] = None) -> None:
        """Start playback of the loaded trajectory.

        Parameters
        ----------
        on_complete : callable, optional
            Called (with no arguments) once the last frame has been applied.
        """
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
            .create_subscription_to_pop(self._on_update, name="blv.trajectory_player")
        )
        carb.log_info(
            f"[BLV] Trajectory playback started — {self.total_frames} frames."
        )

    def stop(self) -> None:
        """Stop playback early (does **not** fire the on_complete callback)."""
        self._playing = False
        self._update_sub = None
        carb.log_info("[BLV] Trajectory playback stopped.")

    # ---- Internal ---------------------------------------------------- #

    def _on_update(self, event) -> None:
        """Advance one frame each update tick."""
        if not self._playing or self._trajectory is None:
            return

        frames = self._trajectory["frames"]

        if self._current_frame >= len(frames):
            # End of trajectory
            self._playing = False
            self._update_sub = None
            carb.log_info("[BLV] Trajectory playback complete.")
            if self._on_complete_callback:
                try:
                    self._on_complete_callback()
                except Exception as exc:
                    carb.log_error(f"[BLV] on_complete callback error: {exc}")
            return

        frame_data = frames[self._current_frame]
        self._controller.set_pose(frame_data["position"], frame_data["rotation"])
        self._current_frame += 1


# ===================================================================== #
#  TrajectoryManager                                                     #
# ===================================================================== #


class TrajectoryManager:
    """Manages a directory of trajectory JSON files.

    Provides convenience methods for listing, loading, and saving trajectories
    without the caller having to deal with file paths directly.  In v2, the
    directory is auto-derived from the project root + environment name:
    ``{root}/{environment}/trajectories/``

    Parameters
    ----------
    directory : str
        Root folder where trajectory ``.json`` files are stored.
    """

    TRAJECTORY_EXT: str = ".json"

    def __init__(self, directory: str = "") -> None:
        self._directory: str = directory

    # ---- Properties -------------------------------------------------- #

    @property
    def directory(self) -> str:
        return self._directory

    @directory.setter
    def directory(self, path: str) -> None:
        self._directory = path

    # ---- Public API -------------------------------------------------- #

    def set_project_paths(
        self, root_folder: str, environment: str, class_name: str = ""
    ) -> str:
        """Set the trajectory directory from project settings.

        Parameters
        ----------
        root_folder : str
            Project root folder (e.g. ``~/blv_data``).
        environment : str
            Environment name (e.g. ``hospital_hallway``).
        class_name : str, optional
            Asset class name.  When provided the directory becomes
            ``{root}/{class}/{env}/trajectories/``.

        Returns
        -------
        str
            The resolved trajectory directory path.
        """
        expanded = os.path.expanduser(root_folder)
        parts = [expanded]
        if class_name:
            parts.append(class_name)
        parts += [environment, "trajectories"]
        self._directory = os.path.join(*parts)
        os.makedirs(self._directory, exist_ok=True)
        carb.log_info(f"[BLV] TrajectoryManager directory → {self._directory}")
        return self._directory

    def list_trajectories(self) -> List[str]:
        """Return sorted list of ``.json`` file paths in the managed directory.

        Returns
        -------
        list[str]
            Absolute paths, sorted alphabetically.
        """
        if not self._directory or not os.path.isdir(self._directory):
            return []
        files = [
            os.path.join(self._directory, f)
            for f in sorted(os.listdir(self._directory))
            if f.endswith(self.TRAJECTORY_EXT)
        ]
        return files

    def list_trajectory_names(self) -> List[str]:
        """Return sorted list of trajectory file **names** (no path)."""
        if not self._directory or not os.path.isdir(self._directory):
            return []
        return sorted(
            f for f in os.listdir(self._directory) if f.endswith(self.TRAJECTORY_EXT)
        )

    def list_trajectory_info(self) -> List[Dict[str, Any]]:
        """Return a list of dicts with name, path, and frame count for each
        trajectory in the directory.

        Returns
        -------
        list[dict]
            Each dict has keys: ``name``, ``path``, ``frame_count``.
        """
        result = []
        for filepath in self.list_trajectories():
            try:
                with open(filepath, "r") as fh:
                    data = json.load(fh)
                result.append({
                    "name": os.path.basename(filepath),
                    "path": filepath,
                    "frame_count": data.get("frame_count", len(data.get("frames", []))),
                })
            except Exception as exc:
                carb.log_warn(f"[BLV] Could not read trajectory {filepath}: {exc}")
                result.append({
                    "name": os.path.basename(filepath),
                    "path": filepath,
                    "frame_count": -1,
                })
        return result

    def load(self, filename: str) -> Dict[str, Any]:
        """Load a trajectory by filename (relative to the managed directory).

        Parameters
        ----------
        filename : str
            e.g. ``"trajectory_001.json"``.

        Returns
        -------
        dict
            Parsed trajectory data.
        """
        filepath = os.path.join(self._directory, filename)
        with open(filepath, "r") as fh:
            data = json.load(fh)
        carb.log_info(f"[BLV] TrajectoryManager loaded {filepath}")
        return data

    def save(self, data: Dict[str, Any], filename: str) -> str:
        """Save trajectory data to the managed directory.

        Parameters
        ----------
        data : dict
            Full trajectory dict (as produced by ``TrajectoryRecorder``).
        filename : str
            Target filename, e.g. ``"trajectory_001.json"``.

        Returns
        -------
        str
            Absolute path of the written file.
        """
        os.makedirs(self._directory, exist_ok=True)
        filepath = os.path.join(self._directory, filename)
        with open(filepath, "w") as fh:
            json.dump(data, fh, indent=2)
        carb.log_info(f"[BLV] TrajectoryManager saved {filepath}")
        return filepath
