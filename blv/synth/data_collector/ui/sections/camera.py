"""Camera Controller section — gamepad enable + speed sliders."""

from __future__ import annotations

from typing import Any, Dict

import omni.ui as ui


class CameraSection:
    def __init__(self, parent_vstack, session, widgets: Dict[str, Any], style) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style

        with ui.CollapsableFrame("Camera Controller", height=0):
            with ui.VStack(spacing=style.SPACING):
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Camera Path:", width=style.LABEL_WIDTH)
                    widgets["cam_path"] = ui.StringField()
                    widgets["cam_path"].model.set_value(session.camera.camera_path)
                    ui.Button(
                        "Set", width=50, clicked_fn=self._on_set_camera_path
                    )

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Move Speed:", width=style.LABEL_WIDTH)
                    widgets["move_speed"] = ui.FloatSlider(min=0.1, max=50.0)
                    widgets["move_speed"].model.set_value(session.camera.move_speed)
                    widgets["move_speed"].model.add_value_changed_fn(
                        lambda m: session.set_move_speed(m.get_value_as_float())
                    )
                    ui.Label("m/s", width=30)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Look Speed:", width=style.LABEL_WIDTH)
                    widgets["look_speed"] = ui.FloatSlider(min=1.0, max=180.0)
                    widgets["look_speed"].model.set_value(session.camera.look_speed)
                    widgets["look_speed"].model.add_value_changed_fn(
                        lambda m: session.set_look_speed(m.get_value_as_float())
                    )
                    ui.Label("deg/s", width=40)

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("Enable Gamepad", clicked_fn=self._on_enable)
                    ui.Button("Disable Gamepad", clicked_fn=self._on_disable)

                widgets["cam_status"] = ui.Label(
                    "Status: Disabled", height=style.FIELD_HEIGHT
                )

    def _on_set_camera_path(self) -> None:
        path = self.widgets["cam_path"].model.get_value_as_string().strip()
        if path:
            self.session.set_camera_path(path)

    def _on_enable(self) -> None:
        try:
            self.session.enable_gamepad()
        except Exception:
            pass

    def _on_disable(self) -> None:
        self.session.disable_gamepad()

    def on_tick(self) -> None:
        enabled = getattr(self.session.camera, "is_enabled", False)
        self.widgets["cam_status"].text = (
            "Status: Enabled" if enabled else "Status: Disabled"
        )

    def destroy(self) -> None:  # pragma: no cover
        return None
