"""Trajectory Recording section — record/stop + frame counter."""

from __future__ import annotations

from typing import Any, Dict

import omni.ui as ui


class TrajectoryRecordSection:
    def __init__(self, parent_vstack, session, widgets: Dict[str, Any], style) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style

        with ui.CollapsableFrame("Trajectory Recording", height=0):
            with ui.VStack(spacing=style.SPACING):
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Trajectory Name:", width=style.LABEL_WIDTH)
                    widgets["traj_name"] = ui.StringField()
                    widgets["traj_name"].model.set_value("trajectory_001")

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("Record", clicked_fn=self._on_start)
                    ui.Button("Stop & Save", clicked_fn=self._on_stop)

                widgets["traj_rec_status"] = ui.Label(
                    "Frames: 0", height=style.FIELD_HEIGHT
                )

    def _on_start(self) -> None:
        name = self.widgets["traj_name"].model.get_value_as_string().strip() or "trajectory"
        self.session.start_trajectory_recording(name)
        self.widgets["traj_rec_status"].text = f"Recording: {name}"

    def _on_stop(self) -> None:
        path = self.session.stop_trajectory_recording()
        if path is None:
            self.widgets["traj_rec_status"].text = "Nothing captured"
        else:
            frames = self.session.traj_recorder.frame_count
            self.widgets["traj_rec_status"].text = (
                f"Saved: {path} ({frames} frames)"
            )

    def on_tick(self) -> None:
        rec = self.session.traj_recorder
        if getattr(rec, "is_recording", False):
            self.widgets["traj_rec_status"].text = (
                f"Recording: {rec.frame_count} frames"
            )

    def destroy(self) -> None:  # pragma: no cover
        return None
