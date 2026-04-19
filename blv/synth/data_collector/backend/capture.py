"""DataRecorder — Replicator-based synthetic data capture.

Wraps Omniverse Replicator's ``BasicWriter`` to capture multi-modal
data from a single camera viewpoint:

* ``rgb`` — standard colour image
* ``semantic_segmentation`` — per-pixel class IDs
* ``colorize_semantic_segmentation`` — human-readable colour overlay
* ``bounding_box_2d_tight`` — axis-aligned bounding boxes for labelled prims

Workflow
--------
1. :meth:`ensure_setup` — idempotent: brings the writer up to date with
   the desired ``(output_dir, resolution, rt_subframes, camera_path,
   annotators)``.  Creates / rebuilds the render product on first call
   or after a stage swap, and swaps only the writer when the output
   directory changes.  This replaces the manual Setup / Teardown
   buttons the UI used to expose.
2. ``await capture_frame()`` — triggers one Replicator step.
3. :meth:`prepare_for_stage_change_async` — called by
   :class:`StageController` before the USD stage is swapped.  Drains
   the orchestrator, detaches the writer, disables the hydra texture,
   destroys the render product.

Important
---------
* ``captureOnPlay`` is disabled so the timeline can run without the
  writer grabbing every physics step — we want explicit, per-frame
  capture only.
* ``rt_subframes`` controls the number of ray-tracing sub-frames per
  step.  4 is a good default for an RTX 5090.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import carb
import omni.kit.app
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
    """Manages Replicator-based data capture for a single camera."""

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
        return self._is_setup

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def output_dir(self) -> str:
        return self._output_dir

    @property
    def camera_path(self) -> str:
        return self._camera_path

    @camera_path.setter
    def camera_path(self, path: str) -> None:
        if self._is_setup and path != self._camera_path:
            carb.log_warn(
                "[BLV] Changing camera_path while the recorder is set up — "
                "the next ensure_setup() call will rebuild the render product."
            )
            self._is_setup = False
        self._camera_path = path

    @property
    def resolution(self) -> Tuple[int, int]:
        return self._resolution

    @resolution.setter
    def resolution(self, res: Tuple[int, int]) -> None:
        if self._is_setup and tuple(res) != tuple(self._resolution):
            self._is_setup = False
        self._resolution = res

    @property
    def rt_subframes(self) -> int:
        return self._rt_subframes

    @rt_subframes.setter
    def rt_subframes(self, val: int) -> None:
        self._rt_subframes = max(1, val)

    @property
    def annotators(self) -> Dict[str, bool]:
        return self._annotators

    @annotators.setter
    def annotators(self, val: Dict[str, bool]) -> None:
        # Annotator change requires a fresh writer.  Mark for rebuild.
        if self._is_setup and dict(val) != dict(self._annotators):
            self._is_setup = False
        self._annotators = dict(val)

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def ensure_setup(
        self,
        output_dir: str,
        resolution: Optional[Tuple[int, int]] = None,
        rt_subframes: Optional[int] = None,
        camera_path: Optional[str] = None,
        annotators: Optional[Dict[str, bool]] = None,
    ) -> None:
        """Idempotent: bring the recorder up to date with the given inputs.

        Called lazily from every capture path — removes the need for a
        user-facing "Setup Writer" button.

        Decision ladder:

        * Not set up (fresh, or just torn down by a stage swap)
          → full :meth:`setup`.
        * Already set up but parameters changed
          → tear down & re-setup.
        * Already set up, only the output directory differs
          → :meth:`reinitialize_writer` (keeps the render product alive).
        * Already set up with the same output_dir
          → no-op.
        """
        if resolution is not None:
            self._resolution = tuple(resolution)
        if rt_subframes is not None:
            self._rt_subframes = max(1, int(rt_subframes))
        if camera_path is not None:
            self._camera_path = camera_path
        if annotators is not None:
            if self._is_setup and dict(annotators) != dict(self._annotators):
                self._release_resources()
            self._annotators = dict(annotators)

        if not self._is_setup:
            self.setup(output_dir, rt_subframes=self._rt_subframes)
            return

        if os.path.normpath(output_dir) != os.path.normpath(self._output_dir):
            self.reinitialize_writer(output_dir)

    def setup(self, output_dir: str, rt_subframes: int = 4) -> None:
        """Create render product, initialise BasicWriter, attach."""
        if self._is_setup:
            carb.log_warn(
                "[BLV] DataRecorder already set up — call teardown first."
            )
            return

        self._output_dir = output_dir
        self._rt_subframes = max(1, rt_subframes)
        os.makedirs(output_dir, exist_ok=True)

        try:
            self._render_product = rep.create.render_product(
                self._camera_path, self._resolution
            )
            rep.orchestrator.set_capture_on_play(False)

            self._writer = self._build_writer(output_dir)
            self._writer.attach([self._render_product])

            self._frame_count = 0
            self._is_setup = True
            carb.log_info(
                f"[BLV] DataRecorder setup complete — output={output_dir}, "
                f"resolution={self._resolution}, rt_subframes={self._rt_subframes}"
            )
        except Exception as exc:  # noqa: BLE001
            carb.log_error(f"[BLV] DataRecorder setup failed: {exc}")
            self._release_resources()
            raise

    def _build_writer(self, output_dir: str):
        writer = rep.writers.get("BasicWriter")
        ann = self._annotators
        writer.initialize(
            output_dir=output_dir,
            rgb=ann.get("rgb", True),
            semantic_segmentation=ann.get("semantic_segmentation", True),
            colorize_semantic_segmentation=ann.get(
                "colorize_semantic_segmentation", True
            ),
            bounding_box_2d_tight=ann.get("bounding_box_2d_tight", True),
            bounding_box_2d_loose=ann.get("bounding_box_2d_loose", False),
            bounding_box_3d=ann.get("bounding_box_3d", False),
            instance_segmentation=ann.get("instance_segmentation", False),
            normals=ann.get("normals", False),
            distance_to_image_plane=ann.get("distance_to_image_plane", False),
            frame_padding=self.FRAME_PADDING,
        )
        return writer

    def reinitialize_writer(self, output_dir: str) -> None:
        """Swap the BasicWriter without destroying the render product."""
        if not self._is_setup or self._render_product is None:
            carb.log_warn(
                "[BLV] reinitialize_writer called but recorder is not set up; "
                "falling back to full setup()."
            )
            self.setup(output_dir, rt_subframes=self._rt_subframes)
            return

        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        if self._writer is not None:
            try:
                self._writer.detach()
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(f"[BLV] Writer detach warning during reinit: {exc}")
            self._writer = None

        self._writer = self._build_writer(output_dir)
        self._writer.attach([self._render_product])
        self._frame_count = 0
        carb.log_info(
            f"[BLV] DataRecorder writer reinitialised → output={output_dir}"
        )

    async def capture_frame(self) -> None:
        """Capture one frame (async — must be awaited)."""
        if not self._is_setup:
            carb.log_error("[BLV] Cannot capture — DataRecorder not set up.")
            return

        try:
            await rep.orchestrator.step_async(
                rt_subframes=self._rt_subframes,
                delta_time=0.0,
                pause_timeline=False,
            )
            self._frame_count += 1
        except Exception as exc:  # noqa: BLE001
            carb.log_error(
                f"[BLV] capture_frame failed at frame {self._frame_count}: {exc}"
            )

    def teardown(self) -> None:
        """Detach the writer, destroy the render product, mark not-setup.

        Safe to call even if setup was never invoked.
        """
        if self._is_setup:
            carb.log_info(
                f"[BLV] DataRecorder teardown — {self._frame_count} frames "
                f"captured to {self._output_dir}"
            )
        self._release_resources(log_warnings=True)

    async def prepare_for_stage_change_async(self) -> None:
        """Drain the orchestrator, detach, and destroy the render product.

        Runs as a pre-close hook from :class:`StageController`.  The
        step ordering here is load-bearing — see the module docstring
        and the refactor plan for why draining-then-destroy is the only
        safe combination.  Skipping any step can leave a dangling
        hydra texture handle that crashes the next orchestrator tick.
        """
        carb.log_info("[BLV] DataRecorder: prepare_for_stage_change_async — begin")

        # 1. Drain in-flight work.
        try:
            await rep.orchestrator.wait_until_complete_async()
            carb.log_info("[BLV] DataRecorder: orchestrator drained")
        except Exception as exc:  # noqa: BLE001
            carb.log_warn(f"[BLV] wait_until_complete_async warning: {exc}")

        # 2. Detach writer first.
        if self._writer is not None:
            try:
                self._writer.detach()
                carb.log_info("[BLV] DataRecorder: writer detached")
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(f"[BLV] Writer detach warning (stage change): {exc}")
            self._writer = None

        # 3. Disable hydra updates, then destroy the render product.
        if self._render_product is not None:
            try:
                hydra_tex = getattr(self._render_product, "hydra_texture", None)
                if hydra_tex is not None:
                    try:
                        hydra_tex.set_updates_enabled(False)
                        carb.log_info("[BLV] DataRecorder: hydra updates disabled")
                    except Exception as exc:  # noqa: BLE001
                        carb.log_warn(
                            f"[BLV] set_updates_enabled(False) warning: {exc}"
                        )
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(f"[BLV] hydra_texture accessor warning: {exc}")

            try:
                self._render_product.destroy()
                carb.log_info("[BLV] DataRecorder: render product destroyed")
            except Exception as exc:  # noqa: BLE001
                carb.log_warn(f"[BLV] render_product.destroy warning: {exc}")
            self._render_product = None

        self._is_setup = False
        self._output_dir = ""

        # 4. Yield a frame so Kit can run C++ destructors.
        try:
            await omni.kit.app.get_app().next_update_async()
        except Exception:  # noqa: BLE001
            pass

        carb.log_info("[BLV] DataRecorder: prepare_for_stage_change_async — done")

    # ------------------------------------------------------------------ #
    #  Internal                                                           #
    # ------------------------------------------------------------------ #

    def _release_resources(self, log_warnings: bool = False) -> None:
        if self._writer is not None:
            try:
                self._writer.detach()
            except Exception as exc:  # noqa: BLE001
                if log_warnings:
                    carb.log_warn(f"[BLV] Writer detach warning: {exc}")
            self._writer = None

        if self._render_product is not None:
            try:
                self._render_product.destroy()
            except Exception as exc:  # noqa: BLE001
                if log_warnings:
                    carb.log_warn(f"[BLV] Render product destroy warning: {exc}")
            self._render_product = None

        self._is_setup = False
        self._output_dir = ""
