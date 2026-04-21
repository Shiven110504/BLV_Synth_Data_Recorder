"""Data Capture section — read-only status.

The old Setup Writer / Teardown buttons are gone — the recorder now
self-manages through :meth:`DataRecorder.ensure_setup`.
"""

from __future__ import annotations

from typing import Any, Dict

import omni.ui as ui


class CaptureSection:
    def __init__(self, parent_vstack, session, widgets: Dict[str, Any], style) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style

        with ui.CollapsableFrame("Data Capture", height=0):
            with ui.VStack(spacing=style.SPACING):
                enabled = [k for k, v in session._annotators.items() if v]
                ann_text = ", ".join(enabled) if enabled else "(none)"
                widgets["data_annotators"] = ui.Label(
                    f"Annotators: {ann_text}",
                    height=style.FIELD_HEIGHT,
                    word_wrap=True,
                )
                widgets["data_status"] = ui.Label(
                    "Status: Not set up | Frames: 0",
                    height=style.FIELD_HEIGHT,
                )

    def on_tick(self) -> None:
        rec = self.session.recorder
        if getattr(rec, "is_setup", False):
            self.widgets["data_status"].text = (
                f"Status: Ready | Frames: {getattr(rec, 'frame_count', 0)}"
            )
        else:
            self.widgets["data_status"].text = "Status: Not set up | Frames: 0"

    def destroy(self) -> None:  # pragma: no cover
        return None
