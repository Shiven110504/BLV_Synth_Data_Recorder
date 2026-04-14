"""
DataCollectorWindow v2 — unified ``omni.ui`` control panel.
============================================================

A single dockable window with collapsible sections for every module in the
BLV Synth Data Collector extension.  v2 redesign:

* **Project Settings** at the top — single root folder + environment derive all
  paths automatically.
* **Trajectory list** — dropdown of trajectories found in the project's
  trajectories folder instead of manual file path entry.
* **Frame sampling** — capture every Nth frame from a trajectory for sparser
  but more diverse datasets.
* All scattered file path inputs replaced with auto-derived paths.

Sections
--------
1. **Project Settings** — root folder, environment, resolution, RT subframes
2. **Camera Controller** — camera path, speed sliders, enable/disable
3. **Trajectory Recording** — name, record/stop, saved trajectory list
4. **Trajectory Playback** — dropdown, play/stop, progress
5. **Data Capture** — annotator info, setup/teardown, frame counter
6. **Record with Trajectory** — dropdown, one-click record, progress
7. **Asset Browser** — folder, class, target prim, prev/next
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import carb
import carb.settings
import omni.kit.app
import omni.ui as ui
import omni.usd
import yaml
from pxr import Gf

from .asset_browser import AssetBrowser
from .data_recorder import DEFAULT_ANNOTATORS, DataRecorder, get_enabled_annotator_names
from .gamepad_camera import GamepadCameraController
from .location import LocationManager
from .trajectory import TrajectoryManager, TrajectoryPlayer, TrajectoryRecorder


def _load_yaml_config() -> Dict[str, Any]:
    """Load the user config YAML shipped next to the extension.toml.

    Returns an empty dict if the file is missing or unparseable.
    """
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        )))),
        "config", "config.yaml",
    )
    if not os.path.isfile(config_path):
        carb.log_info(f"[BLV] No config.yaml found at {config_path} — using defaults.")
        return {}
    try:
        with open(config_path, "r") as fh:
            data = yaml.safe_load(fh)
        carb.log_info(f"[BLV] Loaded config from {config_path}")
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        carb.log_warn(f"[BLV] Failed to parse config.yaml: {exc}")
        return {}

# ===================================================================== #
#  Styling constants                                                     #
# ===================================================================== #

_WINDOW_TITLE = "BLV Synth Data Collector"
_WINDOW_WIDTH = 480
_WINDOW_HEIGHT = 1000
_LABEL_WIDTH = 130
_FIELD_HEIGHT = 22
_BUTTON_HEIGHT = 28
_SPACING = 6


class DataCollectorWindow:
    """Main UI window for the BLV Synth Data Collector extension v2.

    Instantiation creates the ``omni.ui.Window`` and all child widgets.  Call
    :meth:`destroy` to tear everything down cleanly.
    """

    def __init__(self) -> None:
        # ---- Load YAML config (overrides extension.toml defaults) ---- #
        cfg = _load_yaml_config()

        # ---- Read extension settings (fallback) ---------------------- #
        settings = carb.settings.get_settings()
        _ext = "exts.blv.synth.data_collector"

        # Resolution order for every default below:
        #   1. config.yaml value (if the key is present — even if empty/0)
        #   2. carb setting from extension.toml (if non-empty/non-zero)
        #   3. hard-coded fallback baked into the extension
        #
        # Step 1 uses ``key in cfg`` rather than truthiness so an explicit
        # empty string (e.g. ``environment: ""``) is preserved instead of
        # silently falling through to the extension.toml default.
        def _pick_str(key: str, setting_key: str, fallback: str) -> str:
            if key in cfg and cfg[key] is not None:
                return str(cfg[key])
            val = settings.get_as_string(f"/{_ext}/{setting_key}")
            return val if val else fallback

        def _pick_num(key: str, setting_key: str, fallback, as_int: bool):
            if key in cfg and cfg[key] is not None:
                return cfg[key]
            val = (
                settings.get_as_int(f"/{_ext}/{setting_key}")
                if as_int
                else settings.get_as_float(f"/{_ext}/{setting_key}")
            )
            return val if val else fallback

        default_cam = _pick_str("camera_path", "default_camera_path", "/World/BLV_Camera")
        default_move = _pick_num("move_speed", "default_move_speed", 60.0, as_int=False)
        default_look = _pick_num("look_speed", "default_look_speed", 30.0, as_int=False)
        default_focal = _pick_num("focal_length", "default_focal_length", 28.0, as_int=False)
        default_w = _pick_num("resolution_width", "default_resolution_width", 1280, as_int=True)
        default_h = _pick_num("resolution_height", "default_resolution_height", 720, as_int=True)
        default_rt = _pick_num("rt_subframes", "default_rt_subframes", 4, as_int=True)
        default_root = _pick_str("root_folder", "default_root_folder", "~/blv_data")
        # Empty environment / class are valid — the user fills them in the UI
        # and clicks "Apply Settings" before recording.
        default_env = _pick_str("environment", "default_environment", "")
        default_asset_class = _pick_str("asset_class_name", "default_asset_class_name", "")

        # Asset browser defaults from YAML.
        # asset_root_folder is the parent dir; the class name (now a
        # project-level setting) is the subfolder containing USD files.
        # Falls back to legacy 'asset_folder' key for older configs.
        default_asset_root = (
            cfg.get("asset_root_folder")
            or cfg.get("asset_folder", "")
        )
        default_parent_prim = cfg.get("parent_prim_path", "/World")

        # Annotator settings from YAML (merged over built-in defaults)
        annotator_cfg = dict(DEFAULT_ANNOTATORS)
        if "annotators" in cfg and isinstance(cfg["annotators"], dict):
            annotator_cfg.update(cfg["annotators"])

        # ---- Back-end modules ---------------------------------------- #
        self._camera_ctrl = GamepadCameraController(
            camera_prim_path=default_cam,
            move_speed=default_move,
            look_speed=default_look,
            focal_length=default_focal,
        )
        self._traj_recorder = TrajectoryRecorder(self._camera_ctrl)
        # Let the gamepad's X button toggle trajectory recording
        self._camera_ctrl.record_toggle_callback = self._toggle_trajectory_recording
        self._traj_player = TrajectoryPlayer(self._camera_ctrl)
        self._traj_manager = TrajectoryManager()
        self._data_recorder = DataRecorder(
            camera_path=default_cam,
            resolution=(default_w, default_h),
            annotators=annotator_cfg,
        )
        self._asset_browser = AssetBrowser(parent_prim_path=default_parent_prim)
        self._location_manager = LocationManager()
        self._annotator_cfg: Dict[str, bool] = annotator_cfg

        # Project settings state
        self._root_folder: str = default_root
        self._environment: str = default_env
        self._class_name: str = default_asset_class
        self._resolution_w: int = default_w
        self._resolution_h: int = default_h
        self._rt_subframes: int = default_rt

        # Asset browser defaults from config
        self._default_asset_root: str = default_asset_root
        self._default_focal_length: float = default_focal

        # Cached ``default_N`` folder name used when no asset is loaded.
        # Allocated lazily on first capture in a session and reset on
        # writer teardown so the next session picks a fresh number.
        self._default_run_name: Optional[str] = None

        # Async task handle for "Record with Trajectory"
        self._record_traj_task: Optional[asyncio.Task] = None

        # Guard flag to prevent recursive location ComboBox updates
        self._refreshing_locations: bool = False

        # Per-frame UI status updater
        self._status_update_sub = None

        # ---- Build UI ------------------------------------------------ #
        self._window = ui.Window(
            _WINDOW_TITLE,
            width=_WINDOW_WIDTH,
            height=_WINDOW_HEIGHT,
            visible=True,
        )
        self._window.deferred_dock_in("Property", ui.DockPolicy.CURRENT_WINDOW_IS_ACTIVE)

        # Widget references (populated in section builders)
        self._widgets: dict = {}

        with self._window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=_SPACING):
                    self._build_project_settings_section()
                    self._build_camera_section()
                    self._build_trajectory_record_section()
                    self._build_trajectory_play_section()
                    self._build_data_capture_section()
                    self._build_record_with_trajectory_section()
                    self._build_asset_browser_section()

        # Apply initial project paths
        self._apply_project_paths()

        # Start a lightweight per-frame updater to refresh status labels
        self._status_update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_status_update, name="blv.ui_status")
        )

    # ================================================================= #
    #  Window visibility                                                  #
    # ================================================================= #

    @property
    def visible(self) -> bool:
        return self._window.visible if self._window else False

    @visible.setter
    def visible(self, val: bool) -> None:
        if self._window:
            self._window.visible = val

    # ================================================================= #
    #  Helper — Project paths                                             #
    # ================================================================= #

    def _apply_project_paths(self) -> None:
        """Derive all paths from root folder + environment and push to modules.

        Trajectory dir follows the class-scoped structure::

            {root}/{class}/{environment}/[{location}/]trajectories/

        When no class has been set yet, the trajectory manager's directory
        is left cleared; the user will be prompted to set a class before
        any recording is allowed.
        """
        root = self._normalize_path(self._root_folder)
        env = self._environment
        cls = self._current_class_name()
        loc = self._location_manager.current_location

        # Keep the location manager's base directory in sync
        if cls and env:
            self._location_manager.set_base_directory(root, cls, env)

        if cls:
            self._traj_manager.set_project_paths(
                root, env, class_name=cls, location=loc
            )
        else:
            self._traj_manager.directory = ""

        # Refresh location list and trajectory list in UI
        self._refresh_location_list()
        self._refresh_trajectory_lists()

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Expand ``~`` and normalize a filesystem path."""
        return os.path.normpath(os.path.expanduser(path))

    def _get_class_env_dir(self, class_name: str) -> str:
        """Base directory for the current class + environment.

        ``{root}/{class}/{env}`` — class is **required**.
        """
        root = self._normalize_path(self._root_folder)
        return os.path.join(root, class_name, self._environment)

    def _get_location_dir(self, class_name: str) -> str:
        """Base directory scoped to the active location (if any).

        Returns ``{root}/{class}/{env}/{location}`` when a location is
        selected, otherwise falls back to ``{root}/{class}/{env}``.
        """
        base = self._get_class_env_dir(class_name)
        loc = self._location_manager.current_location
        if loc:
            return os.path.join(base, loc)
        return base

    @staticmethod
    def _sanitize_folder_name(name: str) -> str:
        """Make *name* safe to use as a folder component."""
        return "".join(c if (c.isalnum() or c in "_-.") else "_" for c in name)

    def _current_run_name(self, class_name: str) -> str:
        """Return the run-folder name for the current capture session.

        - If an asset is currently loaded in the Asset Browser, the folder
          is named after the asset's filename stem (uniquely identifies
          the asset across the folder).
        - Otherwise, lazily allocate a ``default_N`` by scanning
          the current location/env directory for existing ``default_*``
          folders and picking the next free integer. Cached for the
          session so all captures in one writer setup share one folder;
          reset on ``_on_teardown_writer``.
        """
        stem = self._asset_browser.current_asset_stem
        if stem:
            return self._sanitize_folder_name(stem)

        if self._default_run_name:
            return self._default_run_name

        base = self._get_location_dir(class_name)
        next_n = 1
        if os.path.isdir(base):
            existing = []
            for entry in os.listdir(base):
                if entry.startswith("default_"):
                    try:
                        existing.append(int(entry[len("default_"):]))
                    except ValueError:
                        pass
            if existing:
                next_n = max(existing) + 1
        self._default_run_name = f"default_{next_n}"
        return self._default_run_name

    def _get_capture_output_dir(
        self,
        class_name: str,
        traj_name: str = "",
    ) -> str:
        """Build the capture output directory for the current run.

        Structure::

            {root}/{class}/{env}/[{location}/]{asset_stem|default_N}/[{trajectory}/]

        When an asset is loaded the run folder is the asset's filename
        stem; otherwise a cached ``default_N`` is used. The trajectory
        subfolder is only added when *traj_name* is provided (i.e. for
        Record-with-Trajectory); the manual "Setup Writer" button writes
        directly into the run folder.
        """
        base = self._get_location_dir(class_name)
        run = os.path.join(base, self._current_run_name(class_name))
        if traj_name:
            return os.path.join(run, traj_name)
        return run

    def _repopulate_combo(self, combo_key: str, items: list) -> None:
        """Clear and repopulate a ComboBox widget with *items*."""
        if combo_key not in self._widgets:
            return
        model = self._widgets[combo_key].model
        for child in model.get_item_children(None):
            model.remove_item(child)
        for item in items:
            model.append_child_item(None, ui.SimpleStringModel(item))

    def _refresh_trajectory_lists(self) -> None:
        """Refresh trajectory dropdowns from the trajectory directory."""
        names = self._traj_manager.list_trajectory_names()
        self._repopulate_combo("traj_play_combo", names)
        self._repopulate_combo("rwt_traj_combo", names)

        # Update saved trajectory list label
        if "traj_saved_list" in self._widgets:
            info = self._traj_manager.list_trajectory_info()
            if info:
                lines = []
                for ti in info:
                    fc = ti["frame_count"]
                    fc_str = f"{fc} frames" if fc >= 0 else "error"
                    lines.append(f"  {ti['name']} ({fc_str})")
                self._widgets["traj_saved_list"].text = "\n".join(lines)
            else:
                self._widgets["traj_saved_list"].text = "  (none)"

    # ================================================================= #
    #  Section builders                                                   #
    # ================================================================= #

    # ---- 0. Project Settings ----------------------------------------- #

    def _build_project_settings_section(self) -> None:
        with ui.CollapsableFrame("Project Settings", height=0):
            with ui.VStack(spacing=_SPACING):
                # Root folder
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Root Folder:", width=_LABEL_WIDTH)
                    self._widgets["root_folder"] = ui.StringField()
                    self._widgets["root_folder"].model.set_value(self._root_folder)
                    self._widgets["root_folder"].model.add_end_edit_fn(
                        lambda m: self._on_project_setting_changed()
                    )

                # Environment
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Environment:", width=_LABEL_WIDTH)
                    self._widgets["environment"] = ui.StringField()
                    self._widgets["environment"].model.set_value(self._environment)
                    self._widgets["environment"].model.add_end_edit_fn(
                        lambda m: self._on_project_setting_changed()
                    )

                # Class Name (used for folder layout, asset scan subfolder,
                # and the semantic label applied to swapped assets).
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Class Name:", width=_LABEL_WIDTH)
                    self._widgets["class_name"] = ui.StringField()
                    self._widgets["class_name"].model.set_value(self._class_name)
                    self._widgets["class_name"].model.add_end_edit_fn(
                        lambda m: self._on_project_setting_changed()
                    )

                # Resolution
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Resolution:", width=_LABEL_WIDTH)
                    self._widgets["res_w"] = ui.IntField(width=80)
                    self._widgets["res_w"].model.set_value(self._resolution_w)
                    ui.Label(" x ", width=20, alignment=ui.Alignment.CENTER)
                    self._widgets["res_h"] = ui.IntField(width=80)
                    self._widgets["res_h"].model.set_value(self._resolution_h)

                # RT Subframes
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("RT Subframes:", width=_LABEL_WIDTH)
                    self._widgets["rt_subframes"] = ui.IntField(width=80)
                    self._widgets["rt_subframes"].model.set_value(self._rt_subframes)

                # Apply button
                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "Apply Settings",
                        clicked_fn=self._on_apply_project_settings,
                    )

    # ---- Location callbacks ------------------------------------------ #

    def _refresh_location_list(self) -> None:
        """Repopulate the location ComboBox from disk."""
        if "loc_combo" not in self._widgets:
            return
        # Guard against recursion: modifying the ComboBox model fires
        # the item_changed callback which may call _apply_project_paths
        # which calls us again.
        self._refreshing_locations = True
        try:
            locations = self._location_manager.list_locations()
            self._repopulate_combo("loc_combo", locations)

            # If a location was previously selected and still exists, re-select it
            current = self._location_manager.current_location
            if current and current in locations:
                idx = locations.index(current)
                self._widgets["loc_combo"].model.get_item_value_model().set_value(idx)
            else:
                self._location_manager.current_location = ""
                if not locations:
                    self._widgets["loc_status"].text = "No location selected"
        finally:
            self._refreshing_locations = False

    def _on_location_combo_changed(self, model, item) -> None:
        """Called when the user picks a different location from the dropdown."""
        # Skip if we're programmatically refreshing the dropdown
        if getattr(self, "_refreshing_locations", False):
            return
        current_item = model.get_item_value_model()
        if current_item is None:
            return
        idx = current_item.get_value_as_int()
        locations = self._location_manager.list_locations()
        if idx < 0 or idx >= len(locations):
            return
        name = locations[idx]

        # Save the departing location's current transform before switching
        self._auto_save_location_transform()

        self._switch_to_location(name)

    def _switch_to_location(self, name: str) -> None:
        """Switch to a named location: load its transform, reload the asset,
        and update trajectory paths.

        Uses :meth:`load_asset` (the full delete/recreate cycle) rather than
        ``apply_current_transform`` so that the prim's xformOps are set on a
        clean slate — this is required for unit-conversion scale to resolve
        correctly in the viewport properties panel.
        """
        self._location_manager.current_location = name

        # Load transform from disk
        try:
            data = self._location_manager.load_location(name)
            xform = data.get("spawn_transform", {})
            t = xform.get("translate", [0, 0, 0])
            o = xform.get("orient", [1, 0, 0, 0])
            s = xform.get("scale", [1, 1, 1])

            translate = Gf.Vec3d(*t)
            orient = Gf.Quatd(o[0], o[1], o[2], o[3])
            scale = Gf.Vec3d(*s)

            self._asset_browser.set_spawn_transform(translate, orient, scale)

        except Exception as exc:
            carb.log_warn(f"[BLV] Failed to load location '{name}': {exc}")
            self._widgets["loc_status"].text = f"Error loading location: {exc}"
            self._location_manager.current_location = ""
            return

        # Reload the asset via the full delete/recreate pipeline so
        # xformOps are applied to a fresh prim (matches Prev/Next behaviour).
        # preserve_transform=False tells load_asset NOT to snapshot the
        # current prim's transform (which would overwrite the new location's
        # transform we just set above).
        asset_idx = self._asset_browser.current_index
        if asset_idx >= 0:
            self._sync_asset_browser_fields()
            success = self._asset_browser.load_asset(asset_idx, preserve_transform=False)
            self._update_asset_browser_labels(success)
        else:
            # No asset loaded yet — ensure the scan folder is current, then
            # load the first asset if available.
            root = self._widgets["ab_folder"].model.get_value_as_string().strip()
            if self._expected_scan_folder() != self._asset_browser.asset_folder:
                self._scan_asset_folder(root)
            if self._asset_browser.total_assets > 0:
                self._sync_asset_browser_fields()
                success = self._asset_browser.load_asset(0, preserve_transform=False)
                self._update_asset_browser_labels(success)

        self._update_spawn_transform_fields()

        # Update trajectory paths for this location
        self._apply_project_paths()

        self._widgets["loc_status"].text = f"Location: {name}"
        carb.log_info(f"[BLV] Switched to location '{name}'")

    def _on_new_location_clicked(self) -> None:
        """Show the inline name field for creating a new location."""
        self._widgets["loc_new_row"].visible = True
        self._widgets["loc_name_field"].model.set_value("")

    def _on_cancel_new_location(self) -> None:
        self._widgets["loc_new_row"].visible = False

    def _on_create_location(self) -> None:
        """Validate the name and create a new location with the current transform."""
        name = self._widgets["loc_name_field"].model.get_value_as_string().strip()

        # Need env + class to know where to create the location
        cls = self._current_class_name()
        env = self._current_environment()
        if not cls or not env:
            self._widgets["loc_status"].text = (
                "Error: Set Environment and Class Name first."
            )
            return

        # Ensure the location manager knows the base directory
        self._location_manager.set_base_directory(
            self._normalize_path(self._root_folder), cls, env
        )

        ok, err = self._location_manager.validate_name(name)
        if not ok:
            self._widgets["loc_status"].text = f"Error: {err}"
            return

        # Use the current asset transform (or defaults) as the initial spawn
        xform = self._asset_browser.read_current_prim_transform()
        if xform is not None:
            t, o, s = xform
        else:
            t = self._asset_browser.spawn_translate
            o = self._asset_browser.spawn_orient
            s = self._asset_browser.spawn_scale

        translate = [t[0], t[1], t[2]]
        orient = [o.GetReal(), o.GetImaginary()[0], o.GetImaginary()[1], o.GetImaginary()[2]]
        scale = [s[0], s[1], s[2]]

        try:
            self._location_manager.create_location(name, translate, orient, scale)
        except Exception as exc:
            self._widgets["loc_status"].text = f"Error: {exc}"
            return

        self._widgets["loc_new_row"].visible = False

        # Refresh dropdown and select the new location
        self._location_manager.current_location = name
        self._refresh_location_list()
        self._apply_project_paths()

        # Auto-load the first asset if none is currently loaded
        if not self._asset_browser.current_prim_path:
            root = self._widgets["ab_folder"].model.get_value_as_string().strip()
            if self._expected_scan_folder() != self._asset_browser.asset_folder:
                self._scan_asset_folder(root)
            if self._asset_browser.total_assets > 0:
                self._sync_asset_browser_fields()
                success = self._asset_browser.load_asset(0)
                self._update_asset_browser_labels(success)

        self._widgets["loc_status"].text = f"Created and selected: {name}"
        carb.log_info(f"[BLV] Created location '{name}'")

    def _on_delete_location(self) -> None:
        """Delete the currently selected location."""
        name = self._location_manager.current_location
        if not name:
            self._widgets["loc_status"].text = "No location selected to delete."
            return

        if self._location_manager.delete_location(name):
            self._widgets["loc_status"].text = f"Deleted location: {name}"
            self._refresh_location_list()
            self._apply_project_paths()
        else:
            self._widgets["loc_status"].text = f"Failed to delete '{name}'"

    # ---- 1. Camera Controller ---------------------------------------- #

    def _build_camera_section(self) -> None:
        with ui.CollapsableFrame("Camera Controller", height=0):
            with ui.VStack(spacing=_SPACING):
                # Camera path
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Camera Path:", width=_LABEL_WIDTH)
                    self._widgets["cam_path"] = ui.StringField()
                    self._widgets["cam_path"].model.set_value(
                        self._camera_ctrl.camera_path
                    )
                    ui.Button(
                        "Set", width=50, clicked_fn=self._on_set_camera_path
                    )

                # Move speed
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Move Speed:", width=_LABEL_WIDTH)
                    self._widgets["move_speed"] = ui.FloatSlider(
                        min=0.1, max=50.0
                    )
                    self._widgets["move_speed"].model.set_value(
                        self._camera_ctrl.move_speed
                    )
                    self._widgets["move_speed"].model.add_value_changed_fn(
                        lambda m: setattr(self._camera_ctrl, "move_speed", m.get_value_as_float())
                    )
                    ui.Label("m/s", width=30)

                # Look speed
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Look Speed:", width=_LABEL_WIDTH)
                    self._widgets["look_speed"] = ui.FloatSlider(
                        min=1.0, max=180.0
                    )
                    self._widgets["look_speed"].model.set_value(
                        self._camera_ctrl.look_speed
                    )
                    self._widgets["look_speed"].model.add_value_changed_fn(
                        lambda m: setattr(self._camera_ctrl, "look_speed", m.get_value_as_float())
                    )
                    ui.Label("deg/s", width=40)

                # Enable / Disable buttons
                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "Enable Gamepad",
                        clicked_fn=self._on_enable_gamepad,
                    )
                    ui.Button(
                        "Disable Gamepad",
                        clicked_fn=self._on_disable_gamepad,
                    )

                # Status line
                self._widgets["cam_status"] = ui.Label(
                    "Status: Disabled", height=_FIELD_HEIGHT
                )

    # ---- 2. Trajectory Recording ------------------------------------- #

    def _build_trajectory_record_section(self) -> None:
        with ui.CollapsableFrame("Trajectory Recording", height=0):
            with ui.VStack(spacing=_SPACING):
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Trajectory Name:", width=_LABEL_WIDTH)
                    self._widgets["traj_name"] = ui.StringField()
                    self._widgets["traj_name"].model.set_value("trajectory_001")

                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "Record", clicked_fn=self._on_start_recording
                    )
                    ui.Button(
                        "Stop & Save", clicked_fn=self._on_stop_recording
                    )

                self._widgets["traj_rec_status"] = ui.Label(
                    "Frames: 0", height=_FIELD_HEIGHT
                )

                # Saved trajectories list
                ui.Label("Saved trajectories:", height=_FIELD_HEIGHT)
                self._widgets["traj_saved_list"] = ui.Label(
                    "  (none)", height=0, word_wrap=True
                )

    # ---- 3. Trajectory Playback -------------------------------------- #

    def _build_trajectory_play_section(self) -> None:
        with ui.CollapsableFrame("Trajectory Playback", height=0):
            with ui.VStack(spacing=_SPACING):
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Select:", width=_LABEL_WIDTH)
                    self._widgets["traj_play_combo"] = ui.ComboBox(
                        0, height=_FIELD_HEIGHT
                    )

                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "Play", clicked_fn=self._on_play_trajectory
                    )
                    ui.Button(
                        "Stop", clicked_fn=self._on_stop_trajectory
                    )

                self._widgets["traj_play_progress"] = ui.ProgressBar(
                    height=_FIELD_HEIGHT
                )
                self._widgets["traj_play_progress"].model.set_value(0.0)
                self._widgets["traj_play_frame_lbl"] = ui.Label(
                    "0 / 0", height=_FIELD_HEIGHT
                )

    # ---- 4. Data Capture --------------------------------------------- #

    def _build_data_capture_section(self) -> None:
        with ui.CollapsableFrame("Data Capture", height=0):
            with ui.VStack(spacing=_SPACING):
                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "Setup Writer", clicked_fn=self._on_setup_writer
                    )
                    ui.Button(
                        "Teardown", clicked_fn=self._on_teardown_writer
                    )

                # Show enabled annotators from config
                enabled = get_enabled_annotator_names(self._annotator_cfg)
                ann_text = ", ".join(enabled) if enabled else "(none)"
                self._widgets["data_annotators"] = ui.Label(
                    f"Annotators: {ann_text}",
                    height=_FIELD_HEIGHT,
                    word_wrap=True,
                )

                self._widgets["data_status"] = ui.Label(
                    "Status: Not set up | Frames: 0", height=_FIELD_HEIGHT
                )

    # ---- 5. Record with Trajectory ----------------------------------- #

    def _build_record_with_trajectory_section(self) -> None:
        with ui.CollapsableFrame("Record with Trajectory", height=0):
            with ui.VStack(spacing=_SPACING):
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Trajectory:", width=_LABEL_WIDTH)
                    self._widgets["rwt_traj_combo"] = ui.ComboBox(
                        0, height=_FIELD_HEIGHT
                    )

                # Frame sampling — capture every Nth frame
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Capture every:", width=_LABEL_WIDTH)
                    self._widgets["rwt_frame_step"] = ui.IntField(width=60)
                    self._widgets["rwt_frame_step"].model.set_value(50)
                    ui.Label(" frames  (1 = every frame)", width=180)

                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "Record Trajectory",
                        clicked_fn=self._on_record_with_trajectory,
                    )
                    ui.Button(
                        "Cancel",
                        clicked_fn=self._on_cancel_record_with_trajectory,
                    )

                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "Record All Trajectories",
                        clicked_fn=self._on_record_all_trajectories,
                        tooltip="Record all trajectories at the current location for the loaded asset",
                    )

                self._widgets["rwt_progress"] = ui.ProgressBar(
                    height=_FIELD_HEIGHT
                )
                self._widgets["rwt_progress"].model.set_value(0.0)
                self._widgets["rwt_status"] = ui.Label(
                    "Idle", height=_FIELD_HEIGHT
                )
                self._widgets["rwt_output_label"] = ui.Label(
                    "Output: (not started)", height=_FIELD_HEIGHT, word_wrap=True
                )

    # ---- 6. Asset Browser -------------------------------------------- #

    def _build_asset_browser_section(self) -> None:
        with ui.CollapsableFrame("Asset Browser", height=0):
            with ui.VStack(spacing=_SPACING):

                # ---- Location sub-section (top of Asset Browser) ---- #
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Location:", width=_LABEL_WIDTH)
                    self._widgets["loc_combo"] = ui.ComboBox(0)
                    self._widgets["loc_combo"].model.add_item_changed_fn(
                        self._on_location_combo_changed
                    )

                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "New Location",
                        clicked_fn=self._on_new_location_clicked,
                    )
                    ui.Button(
                        "Delete Location",
                        clicked_fn=self._on_delete_location,
                    )

                # Inline new-location row (hidden by default)
                self._widgets["loc_new_row"] = ui.HStack(
                    height=_FIELD_HEIGHT, visible=False
                )
                with self._widgets["loc_new_row"]:
                    ui.Label("Name:", width=60)
                    self._widgets["loc_name_field"] = ui.StringField()
                    ui.Button("Create", width=60, clicked_fn=self._on_create_location)
                    ui.Button("Cancel", width=60, clicked_fn=self._on_cancel_new_location)

                self._widgets["loc_status"] = ui.Label(
                    "No location selected", height=_FIELD_HEIGHT
                )

                ui.Separator(height=4)

                # ---- Asset Root & Transform ---- #
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Asset Root:", width=_LABEL_WIDTH)
                    self._widgets["ab_folder"] = ui.StringField()
                    self._widgets["ab_folder"].model.set_value(self._default_asset_root)
                    ui.Button("...", width=30, clicked_fn=self._on_browse_asset_folder)

                # Spawn Transform — "From Selection" reads the selected
                # prim's world transform and uses it as the initial spawn
                # position.  Subsequent swaps read the current prim's
                # transform so user adjustments persist.
                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button(
                        "From Selection",
                        clicked_fn=self._on_capture_spawn_transform,
                        tooltip="Capture position/orientation/scale from the selected prim",
                    )

                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Position:", width=_LABEL_WIDTH)
                    for axis in ("x", "y", "z"):
                        ui.Label(f"{axis}:", width=12)
                        self._widgets[f"ab_pos_{axis}"] = ui.FloatField(width=70, read_only=True)
                        self._widgets[f"ab_pos_{axis}"].model.set_value(0.0)

                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Orientation:", width=_LABEL_WIDTH)
                    for axis in ("w", "x", "y", "z"):
                        ui.Label(f"{axis}:", width=12)
                        self._widgets[f"ab_orient_{axis}"] = ui.FloatField(width=55, read_only=True)
                    self._widgets["ab_orient_w"].model.set_value(1.0)

                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Scale:", width=_LABEL_WIDTH)
                    for axis in ("x", "y", "z"):
                        ui.Label(f"{axis}:", width=12)
                        self._widgets[f"ab_scale_{axis}"] = ui.FloatField(width=70, read_only=True)
                        self._widgets[f"ab_scale_{axis}"].model.set_value(1.0)

                with ui.HStack(height=_BUTTON_HEIGHT):
                    ui.Button("Prev", clicked_fn=self._on_prev_asset)
                    ui.Button("Next", clicked_fn=self._on_next_asset)

                self._widgets["ab_status"] = ui.Label(
                    "No folder scanned", height=_FIELD_HEIGHT
                )
                self._widgets["ab_current"] = ui.Label(
                    "Current: None", height=_FIELD_HEIGHT
                )

    # ================================================================= #
    #  Callbacks — Project Settings                                       #
    # ================================================================= #

    def _on_project_setting_changed(self) -> None:
        """Called when root folder, environment, or class fields are edited."""
        self._root_folder = self._widgets["root_folder"].model.get_value_as_string().strip()
        self._environment = self._widgets["environment"].model.get_value_as_string().strip()
        self._class_name = self._widgets["class_name"].model.get_value_as_string().strip()
        # Clear location when project context changes — the new env/class
        # combo may have completely different locations.
        self._location_manager.current_location = ""
        self._apply_project_paths()

    def _on_apply_project_settings(self) -> None:
        """Read all project settings from UI, apply them, and resolve folders.

        Resolves the trajectory directory, refreshes the trajectory list,
        rescans the asset folder for the current class, and pushes the
        class name onto the asset browser as the semantic label.
        """
        self._root_folder = self._widgets["root_folder"].model.get_value_as_string().strip()
        self._environment = self._widgets["environment"].model.get_value_as_string().strip()
        self._class_name = self._widgets["class_name"].model.get_value_as_string().strip()
        self._resolution_w = self._widgets["res_w"].model.get_value_as_int()
        self._resolution_h = self._widgets["res_h"].model.get_value_as_int()
        self._rt_subframes = self._widgets["rt_subframes"].model.get_value_as_int()

        # Update data recorder resolution
        self._data_recorder.resolution = (self._resolution_w, self._resolution_h)
        self._data_recorder.rt_subframes = self._rt_subframes

        # Push class name onto the asset browser so the semantic label is
        # always in sync with the project settings.
        self._asset_browser.class_name = self._class_name

        # Apply paths (resolves trajectory dir, refreshes trajectory/location lists)
        self._apply_project_paths()

        # Rescan the asset folder for the new class subfolder so the
        # browser is ready to swap as soon as the user clicks Prev/Next.
        ab_root = self._widgets.get("ab_folder")
        if ab_root is not None and self._class_name:
            self._scan_asset_folder(ab_root.model.get_value_as_string().strip())

        # If saved locations exist for this env+class, auto-select the first
        # one and load the first asset with its saved transform.
        locations = self._location_manager.list_locations()
        if locations and not self._location_manager.has_location_selected:
            first_loc = locations[0]
            self._switch_to_location(first_loc)
            # Update the ComboBox to reflect the selection
            self._refresh_location_list()

        carb.log_info(
            f"[BLV] Project settings applied: root={self._root_folder}, "
            f"env={self._environment}, class={self._class_name}, "
            f"res={self._resolution_w}x{self._resolution_h}, rt={self._rt_subframes}"
        )

    # ================================================================= #
    #  Callbacks — Camera Controller                                      #
    # ================================================================= #

    def _on_set_camera_path(self) -> None:
        path = self._widgets["cam_path"].model.get_value_as_string().strip()
        if not path:
            carb.log_warn("[BLV] Camera path is empty.")
            return
        self._camera_ctrl.camera_path = path
        # Sync to data recorder only if it's not actively capturing
        if not self._data_recorder.is_setup:
            self._data_recorder.camera_path = path
        else:
            carb.log_info("[BLV] Data recorder active — camera path change deferred to next setup.")
        carb.log_info(f"[BLV] Camera path updated → {path}")

    def _on_enable_gamepad(self) -> None:
        try:
            self._camera_ctrl.enable()
        except Exception as exc:
            carb.log_error(f"[BLV] Enable gamepad failed: {exc}")

    def _on_disable_gamepad(self) -> None:
        self._camera_ctrl.disable()

    # ================================================================= #
    #  Callbacks — Trajectory Recording                                   #
    # ================================================================= #

    def _on_start_recording(self) -> None:
        if not self._require_project_context("traj_rec_status"):
            return

        # Make sure the trajectory manager writes into the class-scoped dir
        # even if the class was changed after window init.
        self._apply_project_paths()

        name = self._widgets["traj_name"].model.get_value_as_string().strip() or "trajectory"
        if not self._camera_ctrl.is_enabled:
            carb.log_warn("[BLV] Enable the gamepad camera before recording.")
        self._traj_recorder.start_recording(
            name=name, environment=self._environment
        )

    def _toggle_trajectory_recording(self) -> None:
        if self._traj_recorder.is_recording:
            self._on_stop_recording()
        else:
            self._on_start_recording()

    def _on_stop_recording(self) -> None:
        if not self._traj_recorder.is_recording:
            carb.log_warn("[BLV] No recording in progress.")
            return

        data = self._traj_recorder.stop_recording()

        # Save to the project's trajectory directory
        name = data.get("name", "trajectory")
        filename = f"{name}.json"

        traj_dir = self._traj_manager.directory
        if traj_dir:
            filepath = os.path.join(traj_dir, filename)
            self._traj_recorder.save_trajectory(filepath)
            self._widgets["traj_rec_status"].text = (
                f"Saved: {filename} ({data['frame_count']} frames)"
            )
            # Refresh trajectory lists so new file appears
            self._refresh_trajectory_lists()
        else:
            self._widgets["traj_rec_status"].text = (
                f"Stopped ({data['frame_count']} frames) — no save folder"
            )

    # ================================================================= #
    #  Callbacks — Trajectory Playback                                    #
    # ================================================================= #

    def _get_selected_trajectory_name(self, combo_key: str) -> str:
        """Get the selected trajectory filename from a ComboBox widget."""
        combo = self._widgets.get(combo_key)
        if combo is None:
            return ""
        model = combo.model
        current_item = model.get_item_value_model()
        if current_item is None:
            return ""
        try:
            idx = current_item.get_value_as_int()
            names = self._traj_manager.list_trajectory_names()
            if 0 <= idx < len(names):
                return names[idx]
        except Exception:
            pass
        return ""

    def _on_play_trajectory(self) -> None:
        traj_name = self._get_selected_trajectory_name("traj_play_combo")
        if not traj_name:
            carb.log_warn("[BLV] No trajectory selected.")
            return

        try:
            filepath = os.path.join(self._traj_manager.directory, traj_name)
            self._traj_player.load_trajectory(filepath)
            self._traj_player.play(on_complete=self._on_playback_complete)
        except Exception as exc:
            carb.log_error(f"[BLV] Failed to load/play trajectory: {exc}")

    def _on_stop_trajectory(self) -> None:
        self._traj_player.stop()

    def _on_playback_complete(self) -> None:
        carb.log_info("[BLV] Trajectory playback finished.")

    # ================================================================= #
    #  Callbacks — Data Capture                                           #
    # ================================================================= #

    def _on_setup_writer(self) -> None:
        """Manual "Setup Writer" button.

        Does NOT require an asset to be loaded in the Asset Browser — this
        is intentional so the user can capture the scene's default asset.
        It DOES require Environment + Class Name to be set, because the
        output path is ``{root}/{class}/{env}/{object}/`` and we refuse to
        scatter data into incomplete folders.
        """
        if not self._require_project_context("data_status"):
            return

        output_dir = self._get_capture_output_dir(class_name=self._current_class_name())

        w = self._widgets["res_w"].model.get_value_as_int()
        h = self._widgets["res_h"].model.get_value_as_int()
        rt = self._widgets["rt_subframes"].model.get_value_as_int()

        self._data_recorder.resolution = (w, h)

        try:
            if self._data_recorder.is_setup:
                self._data_recorder.reinitialize_writer(output_dir)
            else:
                self._data_recorder.setup(output_dir, rt_subframes=rt)
            self._widgets["data_status"].text = (
                f"Status: Writer Ready → {output_dir}"
            )
            carb.log_info(f"[BLV] Writer setup → {output_dir}")
        except Exception as exc:
            self._widgets["data_status"].text = f"Status: Setup failed — {exc}"
            carb.log_error(f"[BLV] Writer setup failed: {exc}")

    def _on_teardown_writer(self) -> None:
        self._data_recorder.teardown()
        self._widgets["data_status"].text = "Status: Not set up | Frames: 0"
        # Drop the cached default_N so the next session picks a fresh number
        self._default_run_name = None

    # ================================================================= #
    #  Callbacks — Record with Trajectory                                 #
    # ================================================================= #

    def _on_record_with_trajectory(self) -> None:
        """Launch the async record-with-trajectory workflow."""
        if self._record_traj_task is not None and not self._record_traj_task.done():
            carb.log_warn("[BLV] A record-with-trajectory session is already running.")
            return

        traj_name = self._get_selected_trajectory_name("rwt_traj_combo")
        if not traj_name:
            carb.log_warn("[BLV] No trajectory selected.")
            self._widgets["rwt_status"].text = "Error: no trajectory selected"
            return

        traj_path = os.path.join(self._traj_manager.directory, traj_name)
        if not os.path.isfile(traj_path):
            carb.log_warn(f"[BLV] Trajectory file not found: {traj_path}")
            self._widgets["rwt_status"].text = "Error: trajectory file not found"
            return

        # Env + Class are required — scope the output under
        # {root}/{class}/{env}/{object}/{trajectory}/
        if not self._require_project_context("rwt_status"):
            return

        traj_stem = os.path.splitext(traj_name)[0]
        output_dir = self._get_capture_output_dir(
            class_name=self._current_class_name(),
            traj_name=traj_stem,
        )

        w = self._widgets["res_w"].model.get_value_as_int()
        h = self._widgets["res_h"].model.get_value_as_int()
        rt = self._widgets["rt_subframes"].model.get_value_as_int()

        self._widgets["rwt_output_label"].text = f"Output: {output_dir}"

        self._record_traj_task = asyncio.ensure_future(
            self._record_with_trajectory_async(traj_path, output_dir, (w, h), rt)
        )

    def _on_cancel_record_with_trajectory(self) -> None:
        """Cancel a running record-with-trajectory session.

        We do NOT teardown the DataRecorder here — the render product stays
        alive so the next "Record Trajectory" run can swap writers cheaply
        via reinitialize_writer().  The user can click the Teardown button
        in the Data Capture section if they want a full reset.
        """
        if self._record_traj_task is not None and not self._record_traj_task.done():
            self._record_traj_task.cancel()
            carb.log_info("[BLV] Record-with-trajectory cancelled by user.")
            self._widgets["rwt_status"].text = "Cancelled"

    async def _record_with_trajectory_async(
        self,
        trajectory_path: str,
        output_dir: str,
        resolution: tuple,
        rt_subframes: int,
    ) -> None:
        """Core async workflow: replay trajectory + capture data at each frame.

        For every frame in the trajectory:
        1. Set the camera pose.
        2. Wait one render frame so the viewport updates.
        3. Capture via Replicator ``step_async``.
        """
        import json

        self._widgets["rwt_status"].text = "Loading trajectory..."
        try:
            with open(trajectory_path, "r") as fh:
                trajectory = json.load(fh)
        except Exception as exc:
            self._widgets["rwt_status"].text = f"Error loading trajectory: {exc}"
            carb.log_error(f"[BLV] record_with_trajectory load error: {exc}")
            return

        frames = trajectory.get("frames", [])
        total = len(frames)
        if total == 0:
            self._widgets["rwt_status"].text = "Error: trajectory has 0 frames"
            return

        # Reuse the persistent self._data_recorder across sessions.  Creating
        # a fresh DataRecorder every recording session and tearing down the
        # render product leaves Replicator's OmniGraph holding stale node
        # handles, which surfaces as "Invalid NodeObj" on the next setup.
        # By keeping one render product alive and only swapping the writer,
        # we sidestep that entirely.
        recorder = self._data_recorder
        if not recorder.is_setup:
            recorder.camera_path = self._camera_ctrl.camera_path
            recorder.resolution = resolution
            recorder.annotators = self._annotator_cfg

        self._widgets["rwt_status"].text = "Setting up writer..."
        try:
            if recorder.is_setup:
                # Fast path: keep render product, just swap the writer's output dir
                recorder.reinitialize_writer(output_dir)
            else:
                recorder.setup(output_dir, rt_subframes=rt_subframes)
        except Exception as exc:
            self._widgets["rwt_status"].text = f"Writer setup failed: {exc}"
            carb.log_error(f"[BLV] record_with_trajectory setup error: {exc}")
            return

        # Read frame step from UI
        frame_step = max(1, self._widgets["rwt_frame_step"].model.get_value_as_int())
        sampled_indices = list(range(0, total, frame_step))
        n_captures = len(sampled_indices)

        self._widgets["rwt_status"].text = (
            f"Recording 0/{n_captures} (sampling every {frame_step} of {total} frames)..."
        )

        try:
            for capture_idx, frame_idx in enumerate(sampled_indices):
                frame_data = frames[frame_idx]

                # 1. Set camera pose
                self._camera_ctrl.set_pose(
                    frame_data["position"], frame_data["rotation"]
                )

                # 2. Wait one render frame so the renderer sees the new pose
                await omni.kit.app.get_app().next_update_async()

                # 3. Capture
                await recorder.capture_frame()

                # Update progress UI
                progress = (capture_idx + 1) / n_captures
                self._widgets["rwt_progress"].model.set_value(progress)
                self._widgets["rwt_status"].text = (
                    f"Recording {capture_idx + 1}/{n_captures} "
                    f"(frame {frame_idx}/{total})..."
                )

        except asyncio.CancelledError:
            carb.log_info("[BLV] record_with_trajectory was cancelled.")
            self._widgets["rwt_status"].text = "Cancelled"
        except Exception as exc:
            carb.log_error(f"[BLV] record_with_trajectory error: {exc}")
            self._widgets["rwt_status"].text = f"Error: {exc}"
        # Note: we deliberately do NOT teardown the recorder here.  The render
        # product is kept alive so the next recording session can reuse it
        # via reinitialize_writer() — see the comment above near setup.

        captured = recorder.frame_count
        self._widgets["rwt_status"].text = (
            f"Done — {captured} frames captured"
        )
        self._widgets["rwt_progress"].model.set_value(1.0)
        carb.log_info(
            f"[BLV] record_with_trajectory complete — {captured} frames → {output_dir}"
        )

    # ================================================================= #
    #  Callbacks — Record All Trajectories                                #
    # ================================================================= #

    def _on_record_all_trajectories(self) -> None:
        """Launch an async workflow that records every trajectory at the
        current location for the currently loaded asset."""
        if self._record_traj_task is not None and not self._record_traj_task.done():
            carb.log_warn("[BLV] A recording session is already running.")
            self._widgets["rwt_status"].text = "Error: recording already in progress"
            return

        if not self._require_project_context("rwt_status"):
            return

        if not self._location_manager.has_location_selected:
            self._widgets["rwt_status"].text = (
                "Error: select a location first (Location section)."
            )
            return

        traj_names = self._traj_manager.list_trajectory_names()
        if not traj_names:
            self._widgets["rwt_status"].text = (
                "Error: no trajectories recorded for this location yet."
            )
            return

        w = self._widgets["res_w"].model.get_value_as_int()
        h = self._widgets["res_h"].model.get_value_as_int()
        rt = self._widgets["rt_subframes"].model.get_value_as_int()

        self._record_traj_task = asyncio.ensure_future(
            self._record_all_trajectories_async(traj_names, (w, h), rt)
        )

    async def _record_all_trajectories_async(
        self,
        traj_names: list,
        resolution: tuple,
        rt_subframes: int,
    ) -> None:
        """Record all trajectories in the current location for the loaded asset.

        Fully automatic: sets up writer, iterates trajectories, captures
        frames, reinitializes writer between trajectories, and tears down
        the writer at the end.
        """
        import json

        total_trajs = len(traj_names)
        frame_step = max(1, self._widgets["rwt_frame_step"].model.get_value_as_int())
        recorder = self._data_recorder
        cls = self._current_class_name()

        overall_captured = 0

        try:
            for traj_idx, traj_name in enumerate(traj_names):
                traj_stem = os.path.splitext(traj_name)[0]
                traj_path = os.path.join(self._traj_manager.directory, traj_name)
                output_dir = self._get_capture_output_dir(
                    class_name=cls, traj_name=traj_stem,
                )

                self._widgets["rwt_status"].text = (
                    f"Trajectory {traj_idx + 1}/{total_trajs}: {traj_stem} — loading..."
                )

                try:
                    with open(traj_path, "r") as fh:
                        trajectory = json.load(fh)
                except Exception as exc:
                    carb.log_error(
                        f"[BLV] Record-all: failed to load {traj_path}: {exc}"
                    )
                    continue

                frames = trajectory.get("frames", [])
                if not frames:
                    carb.log_warn(
                        f"[BLV] Record-all: trajectory {traj_name} has 0 frames, skipping."
                    )
                    continue

                # Setup or reinitialize writer
                if not recorder.is_setup:
                    recorder.camera_path = self._camera_ctrl.camera_path
                    recorder.resolution = resolution
                    recorder.annotators = self._annotator_cfg
                    recorder.setup(output_dir, rt_subframes=rt_subframes)
                else:
                    recorder.reinitialize_writer(output_dir)

                sampled_indices = list(range(0, len(frames), frame_step))
                n_captures = len(sampled_indices)

                for capture_idx, frame_idx in enumerate(sampled_indices):
                    frame_data = frames[frame_idx]
                    self._camera_ctrl.set_pose(
                        frame_data["position"], frame_data["rotation"]
                    )
                    await omni.kit.app.get_app().next_update_async()
                    await recorder.capture_frame()

                    # Progress: combine trajectory-level and frame-level
                    traj_progress = traj_idx / total_trajs
                    frame_progress = (capture_idx + 1) / n_captures / total_trajs
                    self._widgets["rwt_progress"].model.set_value(
                        traj_progress + frame_progress
                    )
                    self._widgets["rwt_status"].text = (
                        f"Trajectory {traj_idx + 1}/{total_trajs}: {traj_stem} — "
                        f"frame {capture_idx + 1}/{n_captures}"
                    )

                overall_captured += n_captures
                carb.log_info(
                    f"[BLV] Record-all: {traj_stem} done ({n_captures} frames → {output_dir})"
                )

        except asyncio.CancelledError:
            carb.log_info("[BLV] Record-all was cancelled.")
            self._widgets["rwt_status"].text = "Cancelled"
        except Exception as exc:
            carb.log_error(f"[BLV] Record-all error: {exc}")
            self._widgets["rwt_status"].text = f"Error: {exc}"
        finally:
            # Full teardown after batch — the session is complete
            recorder.teardown()
            self._default_run_name = None

        self._widgets["rwt_progress"].model.set_value(1.0)
        self._widgets["rwt_status"].text = (
            f"Done — {overall_captured} frames across {total_trajs} trajectories"
        )
        carb.log_info(
            f"[BLV] Record-all complete — {overall_captured} total frames"
        )

    # ================================================================= #
    #  Callbacks — Asset Browser                                          #
    # ================================================================= #

    def _on_browse_asset_folder(self) -> None:
        """Open folder browser for asset directory (uses the existing field)."""
        # In omni.ui there's no built-in folder picker, so we just scan
        # whatever is in the field when the user clicks Prev/Next
        folder = self._widgets["ab_folder"].model.get_value_as_string().strip()
        if folder:
            self._scan_asset_folder(folder)

    def _scan_asset_folder(self, root_folder: str) -> None:
        """Scan ``{root_folder}/{class_name}/`` for USD files.

        The asset browser expects a **root** directory that contains one
        subfolder per class (e.g. ``root/elevator_button/*.usdz``).  The
        class name comes from the Project Settings "Class Name" field and
        doubles as the semantic label applied to loaded assets.
        """
        cls = self._current_class_name()

        # Normalize the root, then compose the actual scan folder
        root_folder = self._normalize_path(root_folder)
        if not cls:
            carb.log_warn("[BLV] Class Name is empty — cannot scan assets.")
            self._widgets["ab_status"].text = "Error: set Class Name first"
            return

        scan_folder = os.path.join(root_folder, cls)
        carb.log_info(
            f"[BLV] Scanning assets: root='{root_folder}', class='{cls}' → '{scan_folder}'"
        )

        if not os.path.isdir(scan_folder):
            carb.log_warn(f"[BLV] Invalid asset folder: '{scan_folder}'")
            self._widgets["ab_status"].text = f"Error: {scan_folder} not found"
            return

        count = self._asset_browser.set_folder(scan_folder, class_name=cls)
        self._widgets["ab_status"].text = f"Found {count} USD files"
        self._widgets["ab_current"].text = "Current: None"

    def _current_class_name(self) -> str:
        """Return the Project Settings Class Name field, stripped."""
        if "class_name" not in self._widgets:
            return self._class_name
        return self._widgets["class_name"].model.get_value_as_string().strip()

    def _current_environment(self) -> str:
        """Return the Project Settings Environment field, stripped."""
        if "environment" not in self._widgets:
            return self._environment
        return self._widgets["environment"].model.get_value_as_string().strip()

    def _require_project_context(self, status_widget_key: str) -> bool:
        """Guard for capture/record actions that need env + class to be set.

        Writes a user-facing error to ``self._widgets[status_widget_key]``
        and logs a warning when either field is empty. Returns True only
        when both are set so callers can early-return on False.
        """
        env = self._current_environment()
        cls = self._current_class_name()
        missing = [n for n, v in (("Environment", env), ("Class Name", cls)) if not v]
        if missing:
            msg = f"{' and '.join(missing)} required — set in Project Settings, then click Apply Settings."
            self._widgets[status_widget_key].text = f"Error: {msg}"
            carb.log_warn(f"[BLV] {msg}")
            return False
        return True

    def _expected_scan_folder(self) -> str:
        """Compose the expected scan folder from the UI root + class fields."""
        root = self._widgets["ab_folder"].model.get_value_as_string().strip()
        cls = self._current_class_name()
        if not root or not cls:
            return ""
        return os.path.join(self._normalize_path(root), cls)

    def _auto_save_location_transform(self) -> None:
        """If a location is active, persist the current transform.

        Reads the live prim xformOps first (most accurate).  Falls back
        to the asset browser's stored spawn transform when no prim is
        loaded — this covers the "From Selection" case where the captured
        transform hasn't been applied to a prim yet.
        """
        if not self._location_manager.has_location_selected:
            return
        xform = self._asset_browser.read_current_prim_transform()
        if xform is not None:
            t, o, s = xform
        else:
            # Fall back to the browser's stored spawn values
            t = self._asset_browser.spawn_translate
            o = self._asset_browser.spawn_orient
            s = self._asset_browser.spawn_scale
        try:
            self._location_manager.save_transform(
                self._location_manager.current_location,
                translate=[t[0], t[1], t[2]],
                orient=[
                    o.GetReal(),
                    o.GetImaginary()[0],
                    o.GetImaginary()[1],
                    o.GetImaginary()[2],
                ],
                scale=[s[0], s[1], s[2]],
            )
        except Exception as exc:
            carb.log_warn(f"[BLV] Auto-save location transform failed: {exc}")

    def _navigate_asset(self, direction: str) -> None:
        """Navigate to the next or previous asset in the browser.

        Handles auto-saving the location transform, auto-scanning when
        the asset root/class has changed, and updating UI labels.
        """
        self._auto_save_location_transform()

        root = self._widgets["ab_folder"].model.get_value_as_string().strip()
        if self._expected_scan_folder() != self._asset_browser.asset_folder:
            self._scan_asset_folder(root)

        self._sync_asset_browser_fields()
        if direction == "next":
            success = self._asset_browser.next_asset()
        else:
            success = self._asset_browser.previous_asset()
        self._update_asset_browser_labels(success)

    def _on_next_asset(self) -> None:
        self._navigate_asset("next")

    def _on_prev_asset(self) -> None:
        self._navigate_asset("prev")

    def _on_capture_spawn_transform(self) -> None:
        """Capture the selected prim's world transform as the spawn position."""
        try:
            sel = omni.usd.get_context().get_selection().get_selected_prim_paths()
        except Exception as exc:
            carb.log_warn(f"[BLV] Could not read viewport selection: {exc}")
            return
        if not sel:
            self._widgets["ab_status"].text = "Select a prim first"
            carb.log_warn("[BLV] No prim selected.")
            return
        if self._asset_browser.capture_transform_from_prim(sel[0]):
            self._update_spawn_transform_fields()
            # Persist to the active location immediately
            self._auto_save_location_transform()
            self._widgets["ab_status"].text = f"Transform captured from {sel[0]}"
        else:
            self._widgets["ab_status"].text = "Failed to capture transform"

    def _sync_asset_browser_fields(self) -> None:
        """Push current UI field values into the AssetBrowser instance."""
        cls = self._current_class_name()
        if cls:
            self._asset_browser.class_name = cls

    def _update_asset_browser_labels(self, success: bool) -> None:
        idx = self._asset_browser.current_index
        total = self._asset_browser.total_assets
        name = self._asset_browser.current_asset_name
        self._widgets["ab_status"].text = f"Asset {idx + 1}/{total}"
        self._widgets["ab_current"].text = f"Current: {name}"
        if success:
            self._update_spawn_transform_fields()

    def _update_spawn_transform_fields(self) -> None:
        """Refresh the read-only fields from the browser's stored spawn transform."""
        self._update_spawn_transform_fields_from(
            self._asset_browser.spawn_translate,
            self._asset_browser.spawn_orient,
            self._asset_browser.spawn_scale,
        )

    def _update_spawn_transform_fields_from(
        self, t: "Gf.Vec3d", o: "Gf.Quatd", s: "Gf.Vec3d"
    ) -> None:
        """Write position/orientation/scale values into the read-only UI fields."""
        self._widgets["ab_pos_x"].model.set_value(t[0])
        self._widgets["ab_pos_y"].model.set_value(t[1])
        self._widgets["ab_pos_z"].model.set_value(t[2])
        self._widgets["ab_orient_w"].model.set_value(o.GetReal())
        self._widgets["ab_orient_x"].model.set_value(o.GetImaginary()[0])
        self._widgets["ab_orient_y"].model.set_value(o.GetImaginary()[1])
        self._widgets["ab_orient_z"].model.set_value(o.GetImaginary()[2])
        self._widgets["ab_scale_x"].model.set_value(s[0])
        self._widgets["ab_scale_y"].model.set_value(s[1])
        self._widgets["ab_scale_z"].model.set_value(s[2])

    # ================================================================= #
    #  Per-frame status updater                                           #
    # ================================================================= #

    def _on_status_update(self, event) -> None:
        """Lightweight per-frame callback to keep status labels current."""
        # Camera controller status
        if self._camera_ctrl.is_enabled:
            slow = "ON" if self._camera_ctrl.slow_mode else "OFF"
            self._widgets["cam_status"].text = (
                f"Status: Enabled | Speed: {self._camera_ctrl.move_speed:.1f} m/s | "
                f"Slow: {slow}"
            )
            # Sync the slider if speed changed via D-pad
            self._widgets["move_speed"].model.set_value(self._camera_ctrl.move_speed)
        else:
            self._widgets["cam_status"].text = "Status: Disabled"

        # Trajectory recording frame count
        if self._traj_recorder.is_recording:
            self._widgets["traj_rec_status"].text = (
                f"Recording... Frames: {self._traj_recorder.frame_count}"
            )

        # Trajectory playback progress
        if self._traj_player.is_playing:
            cur = self._traj_player.current_frame
            total = self._traj_player.total_frames
            self._widgets["traj_play_frame_lbl"].text = f"{cur} / {total}"
            if total > 0:
                self._widgets["traj_play_progress"].model.set_value(cur / total)
        elif self._traj_player.total_frames > 0:
            total = self._traj_player.total_frames
            cur = self._traj_player.current_frame
            self._widgets["traj_play_frame_lbl"].text = f"{cur} / {total}"

        # Data capture status
        if self._data_recorder.is_setup:
            self._widgets["data_status"].text = (
                f"Status: Writer Ready | Frames: {self._data_recorder.frame_count}"
            )

        # Asset browser — live transform display
        xform = self._asset_browser.read_current_prim_transform()
        if xform is not None:
            self._update_spawn_transform_fields_from(xform[0], xform[1], xform[2])

    # ================================================================= #
    #  Teardown                                                           #
    # ================================================================= #

    def destroy(self) -> None:
        """Full teardown — called from the extension's ``on_shutdown``."""
        # Cancel any running async task
        if self._record_traj_task is not None and not self._record_traj_task.done():
            self._record_traj_task.cancel()

        # Drop the status updater
        self._status_update_sub = None

        # Shut down back-end modules
        self._camera_ctrl.destroy()
        self._traj_player.stop()
        self._data_recorder.teardown()

        # Destroy the window
        if self._window is not None:
            self._window.destroy()
            self._window = None

        self._widgets.clear()
        carb.log_info("[BLV] DataCollectorWindow destroyed.")
