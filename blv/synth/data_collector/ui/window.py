"""DataCollectorWindow — thin ``omni.ui`` shell that owns a single Session.

The window creates one :class:`Session` (in
:mod:`blv.synth.data_collector.backend.session`) and forwards every
button click to it.  No asyncio, USD, or filesystem logic lives here —
all of that is in the backend.
"""

from __future__ import annotations

from typing import Any, Dict, List

import carb
import omni.kit.app
import omni.ui as ui

from ..backend.config import load_config
from ..backend.session import Session
from . import style as _style
from .sections.asset_browser import AssetBrowserSection
from .sections.camera import CameraSection
from .sections.capture import CaptureSection
from .sections.collect_all import CollectAllSection
from .sections.project import ProjectSection
from .sections.record_with_trajectory import RecordWithTrajectorySection
from .sections.trajectory_play import TrajectoryPlaySection
from .sections.trajectory_record import TrajectoryRecordSection


class DataCollectorWindow:
    """Single-window UI for the BLV Synth Data Collector extension."""

    def __init__(self) -> None:
        self._window = ui.Window(
            _style.WINDOW_TITLE,
            width=_style.WINDOW_WIDTH,
            height=_style.WINDOW_HEIGHT,
        )

        self._widgets: Dict[str, Any] = {}
        self._sections: List[Any] = []
        self._update_sub = None

        self._session = Session(defaults=load_config())

        with self._window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=_style.SPACING) as root:
                    self._project = ProjectSection(
                        root, self._session, self._widgets, _style,
                        refresh_cb=self._on_project_changed,
                    )
                    self._camera = CameraSection(
                        root, self._session, self._widgets, _style,
                    )
                    self._traj_rec = TrajectoryRecordSection(
                        root, self._session, self._widgets, _style,
                    )
                    self._traj_play = TrajectoryPlaySection(
                        root, self._session, self._widgets, _style,
                    )
                    self._capture = CaptureSection(
                        root, self._session, self._widgets, _style,
                    )
                    self._rwt = RecordWithTrajectorySection(
                        root, self._session, self._widgets, _style,
                    )
                    self._assets = AssetBrowserSection(
                        root, self._session, self._widgets, _style,
                        refresh_cb=self._on_project_changed,
                    )
                    self._collect_all = CollectAllSection(
                        root, self._session, self._widgets, _style,
                    )

        self._sections = [
            self._project, self._camera, self._traj_rec, self._traj_play,
            self._capture, self._rwt, self._assets, self._collect_all,
        ]

        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update, name="blv.ui.tick")
        )

    # ------------------------------------------------------------------ #

    @property
    def visible(self) -> bool:
        return self._window.visible

    @visible.setter
    def visible(self, value: bool) -> None:
        self._window.visible = bool(value)

    def _on_project_changed(self) -> None:
        """Re-populate every section that depends on project paths."""
        for section in (self._traj_play, self._rwt, self._assets):
            if hasattr(section, "refresh"):
                try:
                    section.refresh()
                except Exception as exc:
                    carb.log_warn(f"[BLV] Section refresh failed: {exc}")

    def _on_update(self, event) -> None:
        for section in self._sections:
            try:
                section.on_tick()
            except Exception as exc:
                carb.log_warn(f"[BLV] Section tick failed: {exc}")

    # ------------------------------------------------------------------ #

    def destroy(self) -> None:
        if self._update_sub is not None:
            try:
                self._update_sub.unsubscribe()
            except Exception:
                pass
            self._update_sub = None
        for section in self._sections:
            try:
                section.destroy()
            except Exception:
                pass
        try:
            self._session.destroy()
        except Exception as exc:
            carb.log_warn(f"[BLV] Session destroy failed: {exc}")
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None
