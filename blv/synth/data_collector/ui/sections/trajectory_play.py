"""Trajectory Playback section."""

from __future__ import annotations

from typing import Any, Dict, List

import omni.ui as ui


class TrajectoryPlaySection:
    def __init__(self, parent_vstack, session, widgets: Dict[str, Any], style) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style
        self._names: List[str] = []

        with ui.CollapsableFrame("Trajectory Playback", height=0):
            with ui.VStack(spacing=style.SPACING):
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Select:", width=style.LABEL_WIDTH)
                    widgets["traj_play_combo"] = ui.ComboBox(0, height=style.FIELD_HEIGHT)

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("Play", clicked_fn=self._on_play)
                    ui.Button("Stop", clicked_fn=self._on_stop)

                widgets["traj_play_progress"] = ui.ProgressBar(height=style.FIELD_HEIGHT)
                widgets["traj_play_progress"].model.set_value(0.0)
                widgets["traj_play_frame_lbl"] = ui.Label(
                    "0 / 0", height=style.FIELD_HEIGHT
                )

    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        self._names = self.session.traj_manager.list_trajectory_names()
        _repopulate_combo(self.widgets["traj_play_combo"], self._names)

    def _selected_name(self) -> str:
        idx = self.widgets["traj_play_combo"].model.get_item_value_model().get_value_as_int()
        if 0 <= idx < len(self._names):
            return self._names[idx]
        return ""

    def _on_play(self) -> None:
        name = self._selected_name()
        if not name:
            return
        self.session.play_trajectory(name, on_complete=self._on_playback_complete)

    def _on_stop(self) -> None:
        self.session.stop_trajectory_playback()

    def _on_playback_complete(self) -> None:
        self.widgets["traj_play_frame_lbl"].text = "Playback complete"

    def on_tick(self) -> None:
        player = self.session.traj_player
        cur = getattr(player, "current_frame", 0) or 0
        total = getattr(player, "total_frames", 0) or 0
        fraction = (cur / total) if total else 0.0
        self.widgets["traj_play_progress"].model.set_value(float(fraction))
        if total:
            self.widgets["traj_play_frame_lbl"].text = f"{cur} / {total}"

    def destroy(self) -> None:  # pragma: no cover
        return None


def _repopulate_combo(combo, items: List[str]) -> None:
    model = combo.model
    for child in list(model.get_item_children(None)):
        model.remove_item(child)
    labels = items if items else ["(none)"]
    for label in labels:
        model.append_child_item(None, ui.SimpleStringModel(label))
