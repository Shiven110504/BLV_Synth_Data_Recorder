"""Asset Browser section — location CRUD + asset cycling + Save Transform."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import omni.ui as ui


class AssetBrowserSection:
    def __init__(
        self,
        parent_vstack,
        session,
        widgets: Dict[str, Any],
        style,
        refresh_cb,
    ) -> None:
        self.session = session
        self.widgets = widgets
        self.style = style
        self._refresh_cb = refresh_cb
        self._location_names: List[str] = []
        self._pending_delete: Optional[str] = None

        with ui.CollapsableFrame("Asset Browser", height=0):
            with ui.VStack(spacing=style.SPACING):
                # ---- Location picker + CRUD row ---------------------- #
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Location:", width=style.LABEL_WIDTH)
                    widgets["loc_combo"] = ui.ComboBox(0)
                    widgets["loc_combo"].model.add_item_changed_fn(
                        self._on_location_combo_changed
                    )

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("New Location", clicked_fn=self._on_new_clicked)
                    ui.Button("Save Transform", clicked_fn=self._on_save_transform)
                    ui.Button("Delete", clicked_fn=self._on_delete_clicked)

                # Inline new-location row (hidden by default).
                widgets["loc_new_row"] = ui.HStack(
                    height=style.FIELD_HEIGHT, visible=False
                )
                with widgets["loc_new_row"]:
                    ui.Label("Name:", width=60)
                    widgets["loc_name_field"] = ui.StringField()
                    ui.Button("Create", width=60, clicked_fn=self._on_create)
                    ui.Button("Cancel", width=60, clicked_fn=self._on_cancel_new)

                # Inline delete-confirm row (hidden by default).
                widgets["loc_confirm_row"] = ui.HStack(
                    height=style.FIELD_HEIGHT, visible=False
                )
                with widgets["loc_confirm_row"]:
                    widgets["loc_confirm_label"] = ui.Label(
                        "Delete ''?", width=200
                    )
                    ui.Button(
                        "Confirm Delete", width=120,
                        clicked_fn=self._on_confirm_delete,
                    )
                    ui.Button(
                        "Cancel", width=60, clicked_fn=self._on_cancel_delete,
                    )

                widgets["loc_status"] = ui.Label(
                    "No location selected", height=style.FIELD_HEIGHT
                )

                ui.Separator(height=4)

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("From Selection", clicked_fn=self._on_from_selection)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Position:", width=style.LABEL_WIDTH)
                    for axis in ("x", "y", "z"):
                        ui.Label(f"{axis}:", width=12)
                        widgets[f"ab_pos_{axis}"] = ui.FloatField(width=70, read_only=True)
                        widgets[f"ab_pos_{axis}"].model.set_value(0.0)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Orientation:", width=style.LABEL_WIDTH)
                    for axis in ("w", "x", "y", "z"):
                        ui.Label(f"{axis}:", width=12)
                        widgets[f"ab_orient_{axis}"] = ui.FloatField(width=55, read_only=True)
                    widgets["ab_orient_w"].model.set_value(1.0)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Scale:", width=style.LABEL_WIDTH)
                    for axis in ("x", "y", "z"):
                        ui.Label(f"{axis}:", width=12)
                        widgets[f"ab_scale_{axis}"] = ui.FloatField(width=70, read_only=True)
                        widgets[f"ab_scale_{axis}"].model.set_value(1.0)

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("Prev", clicked_fn=self._on_prev)
                    ui.Button("Next", clicked_fn=self._on_next)

                widgets["ab_status"] = ui.Label(
                    "No folder scanned", height=style.FIELD_HEIGHT
                )
                widgets["ab_current"] = ui.Label(
                    "Current: None", height=style.FIELD_HEIGHT
                )

    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        self._location_names = self.session.locations.list_locations()
        _repopulate_combo(
            self.widgets["loc_combo"],
            self._location_names,
            selected=self.session.locations.current_location,
        )
        current = self.session.locations.current_location
        self.widgets["loc_status"].text = (
            f"Location: {current}" if current else "No location selected"
        )

    # -- Combo changed -------------------------------------------------- #
    def _on_location_combo_changed(self, model, item) -> None:
        current_item = model.get_item_value_model()
        if current_item is None or not self._location_names:
            return
        idx = current_item.get_value_as_int()
        if 0 <= idx < len(self._location_names):
            name = self._location_names[idx]
            self.session.set_location(name)
            self.widgets["loc_status"].text = f"Location: {name}"

    # -- New-location flow --------------------------------------------- #
    def _on_new_clicked(self) -> None:
        self.widgets["loc_new_row"].visible = True
        self.widgets["loc_name_field"].model.set_value("")

    def _on_cancel_new(self) -> None:
        self.widgets["loc_new_row"].visible = False

    def _on_create(self) -> None:
        name = self.widgets["loc_name_field"].model.get_value_as_string().strip()
        if not name:
            self.widgets["loc_status"].text = "Error: name is empty"
            return
        ok, err = self.session.locations.validate_name(name)
        if not ok:
            self.widgets["loc_status"].text = f"Error: {err}"
            return
        try:
            self.session.create_location(name)
        except Exception as exc:
            self.widgets["loc_status"].text = f"Error: {exc}"
            return
        self.widgets["loc_new_row"].visible = False
        self.session.set_location(name)
        self.widgets["loc_status"].text = f"Created and selected: {name}"
        self._refresh_cb()

    # -- Save transform ------------------------------------------------- #
    def _on_save_transform(self) -> None:
        saved = self.session.save_current_transform()
        if saved:
            self.widgets["loc_status"].text = (
                f"Saved transform → {self.session.locations.current_location}"
            )
        else:
            self.widgets["loc_status"].text = "Could not save — no selection"

    # -- Delete flow ---------------------------------------------------- #
    def _on_delete_clicked(self) -> None:
        name = self.session.locations.current_location
        if not name:
            self.widgets["loc_status"].text = "No location selected"
            return
        self._pending_delete = name
        self.widgets["loc_confirm_label"].text = f"Delete '{name}'?"
        self.widgets["loc_confirm_row"].visible = True

    def _on_cancel_delete(self) -> None:
        self._pending_delete = None
        self.widgets["loc_confirm_row"].visible = False

    def _on_confirm_delete(self) -> None:
        name = self._pending_delete
        self.widgets["loc_confirm_row"].visible = False
        self._pending_delete = None
        if not name:
            return
        try:
            deleted = self.session.delete_location(name, confirmed=True)
        except Exception as exc:
            self.widgets["loc_status"].text = f"Error: {exc}"
            return
        self.widgets["loc_status"].text = (
            f"Deleted: {name}" if deleted else f"Failed to delete {name}"
        )
        self._refresh_cb()

    # -- Asset actions -------------------------------------------------- #
    def _on_from_selection(self) -> None:
        import omni.usd
        ctx = omni.usd.get_context()
        selected = ctx.get_selection().get_selected_prim_paths()
        if not selected:
            self.widgets["ab_status"].text = "Nothing selected"
            return
        saved = self.session.assets.capture_transform_from_prim(selected[0])
        if saved:
            self.widgets["ab_status"].text = f"Captured spawn from {selected[0]}"

    def _on_next(self) -> None:
        self.session.assets.next_asset()
        self._update_current_label()

    def _on_prev(self) -> None:
        self.session.assets.previous_asset()
        self._update_current_label()

    def _update_current_label(self) -> None:
        stem = getattr(self.session.assets, "current_asset_stem", "") or "None"
        self.widgets["ab_current"].text = f"Current: {stem}"

    def on_tick(self) -> None:
        snap = self.session.assets.read_current_prim_transform()
        if snap is None:
            return
        t, r, s = snap
        for axis, val in zip(("x", "y", "z"), (t[0], t[1], t[2])):
            self.widgets[f"ab_pos_{axis}"].model.set_value(float(val))
        self.widgets["ab_orient_w"].model.set_value(float(r.GetReal()))
        imag = r.GetImaginary()
        self.widgets["ab_orient_x"].model.set_value(float(imag[0]))
        self.widgets["ab_orient_y"].model.set_value(float(imag[1]))
        self.widgets["ab_orient_z"].model.set_value(float(imag[2]))
        for axis, val in zip(("x", "y", "z"), (s[0], s[1], s[2])):
            self.widgets[f"ab_scale_{axis}"].model.set_value(float(val))

    def destroy(self) -> None:  # pragma: no cover
        return None


def _repopulate_combo(combo, items: List[str], selected: str = "") -> None:
    model = combo.model
    for child in list(model.get_item_children(None)):
        model.remove_item(child)
    labels = items if items else ["(none)"]
    for label in labels:
        model.append_child_item(None, ui.SimpleStringModel(label))
    if selected and selected in items:
        model.get_item_value_model().set_value(items.index(selected))
