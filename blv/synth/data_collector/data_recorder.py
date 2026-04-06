"""
DataRecorder — Replicator-based synthetic data capture.
=======================================================

Wraps Omniverse Replicator's ``BasicWriter`` to capture multi-modal data from a
single camera viewpoint:

* **RGB** — standard colour image
* **semantic_segmentation** — per-pixel class IDs
* **colorize_semantic_segmentation** — human-readable colour overlay
* **bounding_box_2d_tight** — axis-aligned bounding boxes for labelled prims

Workflow
--------
1. ``setup(output_dir, rt_subframes=4)`` — creates a render product from the
   camera, initialises the writer, and attaches it.
2. ``await capture_frame()`` — triggers one Replicator step (async).
3. ``teardown()`` — detaches the writer and destroys the render product.

The ``capture_frame`` method is deliberately async because
``rep.orchestrator.step_async()`` must be awaited.  The "Record with
Trajectory" workflow in the UI calls this in a coroutine.

Important
---------
* ``captureOnPlay`` is disabled during setup so that the timeline can run
  without the writer capturing every physics step — we want explicit,
  per-frame capture only.
* ``rt_subframes`` controls the number of ray-tracing sub-frames rendered
  before data is read back.  Higher values reduce temporal noise at the cost
  of speed.  4 is a good default for RTX 5090.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import carb
import omni.replicator.core as rep


# Default annotator flags — used when no config overrides are provided.
DEFAULT_ANNOTATORS: Dict[str, bool] = {
    "rgb": True,
    "semantic_segmentation": True,
    "colorize_semantic_segmentation": True,
    "bounding_box_2d_tight": True,
    "bounding_box_2d_loose": False,
    "bounding_box_3d": False,
    "instance_segmentation": False,
    "normals": False,
    "distance_to_image_plane": False,
}

# Human-readable names for UI display.
_ANNOTATOR_DISPLAY_NAMES: Dict[str, str] = {
    "rgb": "RGB",
    "semantic_segmentation": "Semantic Segmentation",
    "colorize_semantic_segmentation": "Colorized Semantic Segmentation",
    "bounding_box_2d_tight": "Bounding Box 2D Tight",
    "bounding_box_2d_loose": "Bounding Box 2D Loose",
    "bounding_box_3d": "Bounding Box 3D",
    "instance_segmentation": "Instance Segmentation",
    "normals": "Normals",
    "distance_to_image_plane": "Distance to Image Plane",
}


def get_enabled_annotator_names(annotators: Dict[str, bool]) -> List[str]:
    """Return human-readable names of enabled annotators."""
    return [
        _ANNOTATOR_DISPLAY_NAMES[k]
        for k, v in annotators.items()
        if v and k in _ANNOTATOR_DISPLAY_NAMES
    ]


class DataRecorder:
    """Manages Replicator-based data capture for a single camera.

    Parameters
    ----------
    camera_path : str
        USD prim path of the camera to capture from.
    resolution : tuple[int, int]
        ``(width, height)`` of the captured images.
    """

    # Default frame-number padding in filenames (e.g. ``000042.png``)
    FRAME_PADDING: int = 6

    def __init__(
        self,
        camera_path: str = "/World/BLV_Camera",
        resolution: Tuple[int, int] = (1280, 720),
        annotators: Optional[Dict[str, bool]] = None,
    ) -> None:
        self._camera_path: str = camera_path
        self._resolution: Tuple[int, int] = resolution
        self._annotators: Dict[str, bool] = dict(annotators or DEFAULT_ANNOTATORS)
        self._render_product = None
        self._writer = None
        self._is_setup: bool = False
        self._frame_count: int = 0
        self._output_dir: str = ""
        self._rt_subframes: int = 4

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def is_setup(self) -> bool:
        """``True`` after :meth:`setup` and before :meth:`teardown`."""
        return self._is_setup

    @property
    def frame_count(self) -> int:
        """Number of frames captured since the last :meth:`setup`."""
        return self._frame_count

    @property
    def output_dir(self) -> str:
        """Active output directory (empty string before setup)."""
        return self._output_dir

    @property
    def camera_path(self) -> str:
        return self._camera_path

    @camera_path.setter
    def camera_path(self, path: str) -> None:
        if self._is_setup:
            carb.log_warn(
                "[BLV] Cannot change camera path while writer is active — "
                "teardown first."
            )
            return
        self._camera_path = path

    @property
    def resolution(self) -> Tuple[int, int]:
        return self._resolution

    @resolution.setter
    def resolution(self, res: Tuple[int, int]) -> None:
        if self._is_setup:
            carb.log_warn(
                "[BLV] Cannot change resolution while writer is active — "
                "teardown first."
            )
            return
        self._resolution = res

    @property
    def rt_subframes(self) -> int:
        """Current RT subframes setting."""
        return self._rt_subframes

    @rt_subframes.setter
    def rt_subframes(self, val: int) -> None:
        self._rt_subframes = max(1, val)

    @property
    def annotators(self) -> Dict[str, bool]:
        return self._annotators

    @annotators.setter
    def annotators(self, val: Dict[str, bool]) -> None:
        self._annotators = dict(val)

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def setup(self, output_dir: str, rt_subframes: int = 4) -> None:
        """Create the render product, initialise the BasicWriter, and attach.

        Parameters
        ----------
        output_dir : str
            Directory where captured images / annotations will be written.
            Created automatically if it does not exist.
        rt_subframes : int
            Number of RTX sub-frames rendered per capture step.  Higher values
            reduce noise but take longer.
        """
        if self._is_setup:
            carb.log_warn("[BLV] DataRecorder already set up — call teardown first.")
            return

        self._output_dir = output_dir
        self._rt_subframes = max(1, rt_subframes)
        os.makedirs(output_dir, exist_ok=True)

        try:
            # --- Render product (links camera to output resolution) ---
            self._render_product = rep.create.render_product(
                self._camera_path, self._resolution
            )

            # --- Disable automatic capture during timeline play ---
            rep.orchestrator.set_capture_on_play(False)

            # --- BasicWriter ---
            self._writer = rep.writers.get("BasicWriter")
            ann = self._annotators
            self._writer.initialize(
                output_dir=output_dir,
                rgb=ann.get("rgb", True),
                semantic_segmentation=ann.get("semantic_segmentation", True),
                colorize_semantic_segmentation=ann.get("colorize_semantic_segmentation", True),
                bounding_box_2d_tight=ann.get("bounding_box_2d_tight", True),
                bounding_box_2d_loose=ann.get("bounding_box_2d_loose", False),
                bounding_box_3d=ann.get("bounding_box_3d", False),
                instance_segmentation=ann.get("instance_segmentation", False),
                normals=ann.get("normals", False),
                distance_to_image_plane=ann.get("distance_to_image_plane", False),
                frame_padding=self.FRAME_PADDING,
            )
            self._writer.attach([self._render_product])

            self._frame_count = 0
            self._is_setup = True
            carb.log_info(
                f"[BLV] DataRecorder setup complete — output={output_dir}, "
                f"resolution={self._resolution}, rt_subframes={self._rt_subframes}"
            )

        except Exception as exc:
            carb.log_error(f"[BLV] DataRecorder setup failed: {exc}")
            # Attempt partial cleanup
            self._cleanup_partial()
            raise

    async def capture_frame(self) -> None:
        """Capture a single frame (async).

        Must be called from an ``asyncio`` coroutine or via
        ``asyncio.ensure_future()``.  Each call triggers one Replicator step
        which renders ``rt_subframes`` sub-frames and then reads back the
        annotator data through the attached writer.
        """
        if not self._is_setup:
            carb.log_error("[BLV] Cannot capture — DataRecorder not set up.")
            return

        try:
            await rep.orchestrator.step_async(
                rt_subframes=self._rt_subframes,
                delta_time=0.0,          # don't advance simulation time
                pause_timeline=False,
            )
            self._frame_count += 1
        except Exception as exc:
            carb.log_error(f"[BLV] capture_frame failed at frame {self._frame_count}: {exc}")

    def teardown(self) -> None:
        """Detach the writer and destroy the render product.

        Safe to call even if setup was never called (no-op in that case).
        """
        if self._writer is not None:
            try:
                self._writer.detach()
            except Exception as exc:
                carb.log_warn(f"[BLV] Writer detach warning: {exc}")
            self._writer = None

        if self._render_product is not None:
            try:
                self._render_product.destroy()
            except Exception as exc:
                carb.log_warn(f"[BLV] Render product destroy warning: {exc}")
            self._render_product = None

        if self._is_setup:
            carb.log_info(
                f"[BLV] DataRecorder teardown — {self._frame_count} frames captured "
                f"to {self._output_dir}"
            )
        self._is_setup = False

    # ------------------------------------------------------------------ #
    #  Internal                                                           #
    # ------------------------------------------------------------------ #

    def _cleanup_partial(self) -> None:
        """Best-effort cleanup after a failed setup."""
        try:
            if self._writer is not None:
                self._writer.detach()
        except Exception:
            pass
        self._writer = None

        try:
            if self._render_product is not None:
                self._render_product.destroy()
        except Exception:
            pass
        self._render_product = None
        self._is_setup = False
