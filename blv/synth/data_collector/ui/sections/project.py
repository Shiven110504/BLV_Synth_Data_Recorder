"""Project Settings section — root folder / environment / class / resolution."""

from __future__ import annotations

import os
from typing import Any, Dict, List

import omni.ui as ui


class ProjectSection:
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
        self._initializing = True
        self._env_names: List[str] = []
        self._class_names: List[str] = []

        with ui.CollapsableFrame("Project Settings", height=0):
            with ui.VStack(spacing=style.SPACING):
                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Root Folder:", width=style.LABEL_WIDTH)
                    widgets["root_folder"] = ui.StringField()
                    widgets["root_folder"].model.set_value(session._root_folder)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Environment:", width=style.LABEL_WIDTH)
                    widgets["environment"] = ui.ComboBox(0, height=style.FIELD_HEIGHT)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Class Name:", width=style.LABEL_WIDTH)
                    widgets["class_name"] = ui.ComboBox(0, height=style.FIELD_HEIGHT)

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Environments Folder:", width=style.LABEL_WIDTH)
                    widgets["envs_folder"] = ui.StringField()
                    widgets["envs_folder"].model.set_value(
                        session._defaults.environments_folder
                    )
                    widgets["envs_folder"].model.add_end_edit_fn(
                        lambda m: self.refresh_pickers()
                    )

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Asset Root:", width=style.LABEL_WIDTH)
                    widgets["asset_root"] = ui.StringField()
                    widgets["asset_root"].model.set_value(
                        session._defaults.asset_root_folder
                    )
                    widgets["asset_root"].model.add_end_edit_fn(
                        lambda m: self.refresh_pickers()
                    )

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("Resolution:", width=style.LABEL_WIDTH)
                    widgets["res_w"] = ui.IntField(width=80)
                    widgets["res_w"].model.set_value(session._resolution[0])
                    ui.Label(" x ", width=20, alignment=ui.Alignment.CENTER)
                    widgets["res_h"] = ui.IntField(width=80)
                    widgets["res_h"].model.set_value(session._resolution[1])

                with ui.HStack(height=style.FIELD_HEIGHT):
                    ui.Label("RT Subframes:", width=style.LABEL_WIDTH)
                    widgets["rt_subframes"] = ui.IntField(width=80)
                    widgets["rt_subframes"].model.set_value(session._rt_subframes)

                with ui.HStack(height=style.BUTTON_HEIGHT):
                    ui.Button("Apply Settings", clicked_fn=self._on_apply)

                widgets["project_status"] = ui.Label(
                    "Not applied", height=style.FIELD_HEIGHT
                )

        self.refresh_pickers()
        self._initializing = False

    # ------------------------------------------------------------------ #

    def refresh_pickers(self) -> None:
        envs_root = self.widgets["envs_folder"].model.get_value_as_string().strip()
        self._env_names = _list_subfolders(envs_root) if envs_root else []
        assets_root = self.widgets["asset_root"].model.get_value_as_string().strip()
        self._class_names = _list_subfolders(assets_root) if assets_root else []

        _repopulate_combo(
            self.widgets["environment"],
            self._env_names,
            selected=self.session._environment,
        )
        _repopulate_combo(
            self.widgets["class_name"],
            self._class_names,
            selected=self.session._class_name,
        )

    def _current_env(self) -> str:
        return _combo_selected(self.widgets["environment"], self._env_names)

    def _current_class(self) -> str:
        return _combo_selected(self.widgets["class_name"], self._class_names)

    def _on_apply(self) -> None:
        root = self.widgets["root_folder"].model.get_value_as_string().strip()
        env = self._current_env()
        cls = self._current_class()
        asset_root = self.widgets["asset_root"].model.get_value_as_string().strip()
        res_w = self.widgets["res_w"].model.get_value_as_int()
        res_h = self.widgets["res_h"].model.get_value_as_int()
        rt = self.widgets["rt_subframes"].model.get_value_as_int()

        self.session.apply_project_settings(
            root_folder=root,
            environment=env,
            class_name=cls,
            resolution=(res_w, res_h),
            rt_subframes=rt,
        )
        self.session.assets.class_name = cls

        # Scan the asset folder for the current class so Prev/Next has
        # something to iterate.  Matches old ui.py _scan_asset_folder.
        if asset_root and cls:
            scan_dir = os.path.join(
                os.path.expanduser(asset_root), cls,
            )
            try:
                n = self.session.assets.set_folder(scan_dir, class_name=cls)
                if "ab_status" in self.widgets:
                    self.widgets["ab_status"].text = (
                        f"Scanned {scan_dir}: {n} assets"
                    )
            except Exception as exc:
                if "ab_status" in self.widgets:
                    self.widgets["ab_status"].text = f"Scan error: {exc}"

        # Auto-select first location if none is selected — triggers the
        # trajectory manager to rebind to that location's trajectories
        # dir.  Matches old ui.py _on_apply_project_settings.
        locations = self.session.locations.list_locations()
        if locations and not self.session.locations.has_location_selected:
            self.session.set_location(locations[0])

        # Load the first location's saved spawn transform and the first
        # scanned asset so the user has something visible the moment
        # Apply finishes.
        loc = self.session.locations.current_location
        if loc:
            try:
                data = self.session.locations.load_location(loc)
                from pxr import Gf
                t = data["spawn_transform"]["translate"]
                r = data["spawn_transform"]["orient"]
                s = data["spawn_transform"]["scale"]
                self.session.assets.set_spawn_transform(
                    Gf.Vec3d(*t),
                    Gf.Quatd(r[0], r[1], r[2], r[3]),
                    Gf.Vec3d(*s),
                )
            except Exception:
                pass
        if (
            self.session.assets.total_assets > 0
            and self.session.locations.has_location_selected
        ):
            try:
                self.session.assets.load_asset(0, preserve_transform=False)
            except Exception as exc:
                if "ab_status" in self.widgets:
                    self.widgets["ab_status"].text = (
                        f"Asset load error: {exc}"
                    )

        self.widgets["project_status"].text = (
            f"env={env or '(none)'} | class={cls or '(none)'} | "
            f"{res_w}x{res_h} @ RT{rt}"
        )
        self._refresh_cb()

    def on_tick(self) -> None:  # pragma: no cover — UI-only
        return None

    def destroy(self) -> None:  # pragma: no cover
        return None


# ---------------------------------------------------------------------- #
#  ComboBox + filesystem helpers                                          #
# ---------------------------------------------------------------------- #

def _list_subfolders(path: str) -> List[str]:
    path = os.path.expanduser(path) if path else ""
    if not path or not os.path.isdir(path):
        return []
    try:
        return sorted(
            name
            for name in os.listdir(path)
            if os.path.isdir(os.path.join(path, name))
        )
    except OSError:
        return []


def _repopulate_combo(combo, items: List[str], selected: str = "") -> None:
    model = combo.model
    # Clear existing items.
    for child in list(model.get_item_children(None)):
        model.remove_item(child)
    labels = items if items else ["(none)"]
    for label in labels:
        model.append_child_item(None, ui.SimpleStringModel(label))
    if selected and selected in items:
        model.get_item_value_model().set_value(items.index(selected))


def _combo_selected(combo, items: List[str]) -> str:
    if not items:
        return ""
    idx = combo.model.get_item_value_model().get_value_as_int()
    if 0 <= idx < len(items):
        return items[idx]
    return ""
