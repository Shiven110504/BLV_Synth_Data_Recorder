"""Collect All Data section — runs the env × location × asset × trajectory matrix.

Renamed from the previous "Brainrot Mode" section, the description copy is
gone, and the button simply reads "Start".
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import omni.ui as ui


class CollectAllSection:
    def __init__(self, parent_vstack, session, widgets: Dict[str, Any], style) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style
        self._task: Optional[asyncio.Task] = None

        with ui.CollapsableFrame("Collect All Data", height=0, collapsed=True):
            with ui.VStack(spacing=style.SPACING):
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Environments Folder:", width=style.LABEL_WIDTH)
                    widgets["collect_all_envs_folder"] = ui.StringField()
                    widgets["collect_all_envs_folder"].model.set_value(
                        session._defaults.environments_folder
                    )
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Capture every:", width=style.LABEL_WIDTH)
                    widgets["collect_all_frame_step"] = ui.IntField(width=60)
                    widgets["collect_all_frame_step"].model.set_value(50)
                    ui.Label(" frames", width=80)
                with ui.HStack(height=style.BUTTON_HEIGHT):
                    widgets["collect_all_start_btn"] = ui.Button(
                        "Start", clicked_fn=self._on_start,
                    )
                    widgets["collect_all_cancel_btn"] = ui.Button(
                        "Cancel", clicked_fn=self._on_cancel,
                    )
                widgets["collect_all_progress"] = ui.ProgressBar(
                    height=style.FIELD_HEIGHT
                )
                widgets["collect_all_progress"].model.set_value(0.0)
                widgets["collect_all_status"] = ui.Label(
                    "Idle", height=style.FIELD_HEIGHT, word_wrap=True,
                )

    def _progress_cb(self, fraction, status, detail=""):
        if fraction is not None:
            self.widgets["collect_all_progress"].model.set_value(float(fraction))
        text = status if not detail else f"{status} — {detail}"
        self.widgets["collect_all_status"].text = text

    def _on_start(self) -> None:
        envs_folder = self.widgets["collect_all_envs_folder"].model.get_value_as_string().strip()
        step = max(1, self.widgets["collect_all_frame_step"].model.get_value_as_int() or 1)
        if not envs_folder:
            self.widgets["collect_all_status"].text = "Set the environments folder first"
            return

        async def run():
            try:
                captured = await self.session.collect_all(
                    envs_folder=envs_folder,
                    frame_step=step,
                    progress_cb=self._progress_cb,
                )
                self.widgets["collect_all_status"].text = (
                    f"Done — captured {captured} frames"
                )
            except asyncio.CancelledError:
                self.widgets["collect_all_status"].text = "Cancelled"
            except Exception as exc:
                self.widgets["collect_all_status"].text = f"Error: {exc}"

        self._task = asyncio.ensure_future(run())

    def _on_cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def on_tick(self) -> None:  # pragma: no cover
        return None

    def destroy(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
