"""Camera Controller section — gamepad enable + speed sliders."""

from __future__ import annotations

from typing import Any, Dict

import omni.ui as ui


class CameraSection:
    def __init__(self, parent_vstack, session, widgets: Dict[str, Any], style) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style
        self._syncing: bool = False

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
                        self._on_move_speed_changed
                    )
                    ui.Label("m/s", width=30)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Look Speed:", width=style.LABEL_WIDTH)
                    widgets["look_speed"] = ui.FloatSlider(min=1.0, max=180.0)
                    widgets["look_speed"].model.set_value(session.camera.look_speed)
                    widgets["look_speed"].model.add_value_changed_fn(
                        self._on_look_speed_changed
                    )
                    ui.Label("deg/s", width=40)

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("Enable Gamepad", clicked_fn=self._on_enable)
                    ui.Button("Disable Gamepad", clicked_fn=self._on_disable)

                widgets["cam_status"] = ui.Label(
                    "Status: Disabled", height=style.FIELD_HEIGHT
                )
                widgets["cam_slow_mode"] = ui.Label(
                    "Slow Mode: OFF", height=style.FIELD_HEIGHT
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

    def _on_move_speed_changed(self, model) -> None:
        if self._syncing:
            return
        self.session.set_move_speed(model.get_value_as_float())

    def _on_look_speed_changed(self, model) -> None:
        if self._syncing:
            return
        self.session.set_look_speed(model.get_value_as_float())

    def on_tick(self) -> None:
        cam = self.session.camera
        enabled = getattr(cam, "is_enabled", False)
        self.widgets["cam_status"].text = (
            "Status: Enabled" if enabled else "Status: Disabled"
        )
        slow = getattr(cam, "slow_mode", False)
        self.widgets["cam_slow_mode"].text = (
            "Slow Mode: ON (0.5×)" if slow else "Slow Mode: OFF"
        )
        # Push backend speed values back onto the sliders so D-pad
        # changes show up in the UI.
        self._syncing = True
        try:
            model = self.widgets["move_speed"].model
            if abs(model.get_value_as_float() - cam.move_speed) > 1e-3:
                model.set_value(float(cam.move_speed))
            model = self.widgets["look_speed"].model
            if abs(model.get_value_as_float() - cam.look_speed) > 1e-3:
                model.set_value(float(cam.look_speed))
        finally:
            self._syncing = False

    def destroy(self) -> None:  # pragma: no cover
        return None
