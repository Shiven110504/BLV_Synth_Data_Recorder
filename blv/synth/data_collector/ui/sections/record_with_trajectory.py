"""Record with Trajectory section — one-click trajectory → capture.

Also hosts the ``Record All Trajectories`` button that iterates every
trajectory at the current location against every asset.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import omni.ui as ui


class RecordWithTrajectorySection:
    def __init__(self, parent_vstack, session, widgets: Dict[str, Any], style) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style
        self._names: List[str] = []
        self._task: Optional[asyncio.Task] = None

        with ui.CollapsableFrame("Record with Trajectory", height=0):
            with ui.VStack(spacing=style.SPACING):
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Trajectory:", width=style.LABEL_WIDTH)
                    widgets["rwt_traj_combo"] = ui.ComboBox(0, height=style.FIELD_HEIGHT)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Capture every:", width=style.LABEL_WIDTH)
                    widgets["rwt_frame_step"] = ui.IntField(width=60)
                    widgets["rwt_frame_step"].model.set_value(50)
                    ui.Label(" frames  (1 = every frame)", width=180)

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    widgets["rwt_record_btn"] = ui.Button(
                        "Record Trajectory", clicked_fn=self._on_record
                    )
                    widgets["rwt_cancel_btn"] = ui.Button(
                        "Cancel", clicked_fn=self._on_cancel
                    )

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    widgets["rwt_record_all_btn"] = ui.Button(
                        "Record All Trajectories",
                        clicked_fn=self._on_record_all,
                        tooltip=(
                            "Record every trajectory at the current location "
                            "against every scanned asset"
                        ),
                    )

                widgets["rwt_progress"] = ui.ProgressBar(height=style.FIELD_HEIGHT)
                widgets["rwt_progress"].model.set_value(0.0)
                widgets["rwt_status"] = ui.Label("Idle", height=style.FIELD_HEIGHT)
                widgets["rwt_output_label"] = ui.Label(
                    "Output: (not started)", height=style.FIELD_HEIGHT,
                    word_wrap=True,
                )

    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        self._names = self.session.traj_manager.list_trajectory_names()
        _repopulate_combo(self.widgets["rwt_traj_combo"], self._names)

    def _selected_name(self) -> str:
        idx = self.widgets["rwt_traj_combo"].model.get_item_value_model().get_value_as_int()
        if 0 <= idx < len(self._names):
            return self._names[idx]
        return ""

    def _progress_cb(self, fraction, status, detail=""):
        if fraction is not None:
            self.widgets["rwt_progress"].model.set_value(float(fraction))
        text = status if not detail else f"{status} — {detail}"
        self.widgets["rwt_status"].text = text

    def _on_record(self) -> None:
        name = self._selected_name()
        if not name:
            self.widgets["rwt_status"].text = "Pick a trajectory first"
            return
        step = max(1, self.widgets["rwt_frame_step"].model.get_value_as_int() or 1)
        self.widgets["rwt_output_label"].text = (
            f"Output: {self.session.capture_output_dir(name.rsplit('.', 1)[0])}"
        )

        async def run():
            try:
                captured = await self.session.record_with_trajectory(
                    name, frame_step=step, progress_cb=self._progress_cb,
                )
                self.widgets["rwt_status"].text = (
                    f"Done — captured {captured} frames"
                )
            except asyncio.CancelledError:
                self.widgets["rwt_status"].text = "Cancelled"
            except Exception as exc:
                self.widgets["rwt_status"].text = f"Error: {exc}"

        self._task = asyncio.ensure_future(run())

    def _on_record_all(self) -> None:
        step = max(1, self.widgets["rwt_frame_step"].model.get_value_as_int() or 1)

        async def run():
            try:
                captured = await self.session.record_all_trajectories(
                    frame_step=step, progress_cb=self._progress_cb,
                )
                self.widgets["rwt_status"].text = (
                    f"Done — captured {captured} total frames"
                )
            except asyncio.CancelledError:
                self.widgets["rwt_status"].text = "Cancelled"
            except Exception as exc:
                self.widgets["rwt_status"].text = f"Error: {exc}"

        self._task = asyncio.ensure_future(run())

    def _on_cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def on_tick(self) -> None:  # pragma: no cover
        return None

    def destroy(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


def _repopulate_combo(combo, items: List[str]) -> None:
    model = combo.model
    for child in list(model.get_item_children(None)):
        model.remove_item(child)
    labels = items if items else ["(none)"]
    for label in labels:
        model.append_child_item(None, ui.SimpleStringModel(label))
