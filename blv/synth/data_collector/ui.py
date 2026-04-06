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
import yaml

from .asset_browser import AssetBrowser
from .data_recorder import DEFAULT_ANNOTATORS, DataRecorder, get_enabled_annotator_names
from .gamepad_camera import GamepadCameraController
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

        default_cam = cfg.get("camera_path") or settings.get_as_string(
            f"/{_ext}/default_camera_path"
        ) or "/World/BLV_Camera"
        default_move = cfg.get("move_speed") or settings.get_as_float(
            f"/{_ext}/default_move_speed"
        ) or 60.0
        default_look = cfg.get("look_speed") or settings.get_as_float(
            f"/{_ext}/default_look_speed"
        ) or 30.0
        default_w = cfg.get("resolution_width") or settings.get_as_int(
            f"/{_ext}/default_resolution_width"
        ) or 1280
        default_h = cfg.get("resolution_height") or settings.get_as_int(
            f"/{_ext}/default_resolution_height"
        ) or 720
        default_rt = cfg.get("rt_subframes") or settings.get_as_int(
            f"/{_ext}/default_rt_subframes"
        ) or 4
        default_root = cfg.get("root_folder") or settings.get_as_string(
            f"/{_ext}/default_root_folder"
        ) or "~/blv_data"
        default_env = cfg.get("environment") or settings.get_as_string(
            f"/{_ext}/default_environment"
        ) or "hospital_hallway"

        # Asset browser defaults from YAML
        default_asset_folder = cfg.get("asset_folder", "")
        default_asset_class = cfg.get("asset_class_name", "")
        default_target_prim = cfg.get("target_prim_path", "/World/TargetAsset")

        # Annotator settings from YAML (merged over built-in defaults)
        annotator_cfg = dict(DEFAULT_ANNOTATORS)
        if "annotators" in cfg and isinstance(cfg["annotators"], dict):
            annotator_cfg.update(cfg["annotators"])

        # ---- Back-end modules ---------------------------------------- #
        self._camera_ctrl = GamepadCameraController(
            camera_prim_path=default_cam,
            move_speed=default_move,
            look_speed=default_look,
        )
        self._traj_recorder = TrajectoryRecorder(self._camera_ctrl)
        self._traj_player = TrajectoryPlayer(self._camera_ctrl)
        self._traj_manager = TrajectoryManager()
        self._data_recorder = DataRecorder(
            camera_path=default_cam,
            resolution=(default_w, default_h),
            annotators=annotator_cfg,
        )
        self._asset_browser = AssetBrowser()
        self._annotator_cfg: Dict[str, bool] = annotator_cfg

        # Project settings state
        self._root_folder: str = default_root
        self._environment: str = default_env
        self._resolution_w: int = default_w
        self._resolution_h: int = default_h
        self._rt_subframes: int = default_rt

        # Asset browser defaults from config
        self._default_asset_folder: str = default_asset_folder
        self._default_asset_class: str = default_asset_class
        self._default_target_prim: str = default_target_prim

        # Async task handle for "Record with Trajectory"
        self._record_traj_task: Optional[asyncio.Task] = None

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
        """Derive all paths from root folder + environment and push to modules."""
        root = os.path.expanduser(self._root_folder)
        env = self._environment

        # Trajectory manager
        self._traj_manager.set_project_paths(root, env)

        # Refresh trajectory list in UI
        self._refresh_trajectory_lists()

    def _get_capture_output_dir(
        self,
        class_name: str = "",
        asset_name: str = "",
        traj_name: str = "",
    ) -> str:
        """Build the capture output directory from project settings.

        Structure: {root}/{environment}/captures/{class}_{asset}/{traj}/
        """
        root = os.path.expanduser(self._root_folder)
        env = self._environment
        parts = [root, env, "captures"]
        if class_name and asset_name:
            parts.append(f"{class_name}_{asset_name}")
        elif class_name:
            parts.append(class_name)
        if traj_name:
            parts.append(traj_name)
        return os.path.join(*parts)

    def _refresh_trajectory_lists(self) -> None:
        """Refresh trajectory dropdowns from the trajectory directory."""
        names = self._traj_manager.list_trajectory_names()

        # Update playback dropdown
        if "traj_play_combo" in self._widgets:
            combo = self._widgets["traj_play_combo"]
            model = combo.model
            children = model.get_item_children(None)
            for child in children:
                model.remove_item(child)
            for name in names:
                model.append_child_item(None, ui.SimpleStringModel(name))

        # Update record-with-trajectory dropdown
        if "rwt_traj_combo" in self._widgets:
            combo = self._widgets["rwt_traj_combo"]
            model = combo.model
            children = model.get_item_children(None)
            for child in children:
                model.remove_item(child)
            for name in names:
                model.append_child_item(None, ui.SimpleStringModel(name))

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
                        min=0.1, max=200.0
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
                    self._widgets["rwt_frame_step"].model.set_value(1)
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
                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Asset Folder:", width=_LABEL_WIDTH)
                    self._widgets["ab_folder"] = ui.StringField()
                    self._widgets["ab_folder"].model.set_value(self._default_asset_folder)
                    ui.Button("...", width=30, clicked_fn=self._on_browse_asset_folder)

                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Class Name:", width=_LABEL_WIDTH)
                    self._widgets["ab_class"] = ui.StringField()
                    self._widgets["ab_class"].model.set_value(self._default_asset_class)

                with ui.HStack(height=_FIELD_HEIGHT):
                    ui.Label("Target Prim:", width=_LABEL_WIDTH)
                    self._widgets["ab_target"] = ui.StringField()
                    self._widgets["ab_target"].model.set_value(self._default_target_prim)
                    ui.Button("Pick", width=50, clicked_fn=self._on_pick_target_prim)

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
        """Called when root folder or environment fields are edited."""
        self._root_folder = self._widgets["root_folder"].model.get_value_as_string().strip()
        self._environment = self._widgets["environment"].model.get_value_as_string().strip()
        self._apply_project_paths()

    def _on_apply_project_settings(self) -> None:
        """Read all project settings from UI and apply them."""
        self._root_folder = self._widgets["root_folder"].model.get_value_as_string().strip()
        self._environment = self._widgets["environment"].model.get_value_as_string().strip()
        self._resolution_w = self._widgets["res_w"].model.get_value_as_int()
        self._resolution_h = self._widgets["res_h"].model.get_value_as_int()
        self._rt_subframes = self._widgets["rt_subframes"].model.get_value_as_int()

        # Update data recorder resolution
        self._data_recorder.resolution = (self._resolution_w, self._resolution_h)
        self._data_recorder.rt_subframes = self._rt_subframes

        # Update data capture info labels
        if "data_res_label" in self._widgets:
            self._widgets["data_res_label"].text = (
                f"Resolution: {self._resolution_w} x {self._resolution_h}"
            )
        if "data_rt_label" in self._widgets:
            self._widgets["data_rt_label"].text = (
                f"RT Subframes: {self._rt_subframes}"
            )

        # Apply paths
        self._apply_project_paths()
        carb.log_info(
            f"[BLV] Project settings applied: root={self._root_folder}, "
            f"env={self._environment}, res={self._resolution_w}x{self._resolution_h}, "
            f"rt={self._rt_subframes}"
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
        # Sync to data recorder as well
        self._data_recorder.camera_path = path
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
        name = self._widgets["traj_name"].model.get_value_as_string().strip() or "trajectory"
        if not self._camera_ctrl.is_enabled:
            carb.log_warn("[BLV] Enable the gamepad camera before recording.")
        self._traj_recorder.start_recording(
            name=name, environment=self._environment
        )

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
        output_dir = self._get_capture_output_dir()
        if not output_dir:
            carb.log_warn("[BLV] Cannot determine output directory.")
            return

        w = self._widgets["res_w"].model.get_value_as_int()
        h = self._widgets["res_h"].model.get_value_as_int()
        rt = self._widgets["rt_subframes"].model.get_value_as_int()

        self._data_recorder.resolution = (w, h)

        try:
            self._data_recorder.setup(output_dir, rt_subframes=rt)
            self._widgets["data_status"].text = (
                f"Status: Writer Ready | Frames: 0"
            )
        except Exception as exc:
            self._widgets["data_status"].text = f"Status: Setup failed — {exc}"
            carb.log_error(f"[BLV] Writer setup failed: {exc}")

    def _on_teardown_writer(self) -> None:
        self._data_recorder.teardown()
        self._widgets["data_status"].text = "Status: Not set up | Frames: 0"

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

        # Build output dir from project settings + asset info
        class_name = self._asset_browser.class_name
        asset_stem = self._asset_browser.current_asset_stem
        traj_stem = os.path.splitext(traj_name)[0]
        output_dir = self._get_capture_output_dir(
            class_name=class_name,
            asset_name=asset_stem,
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
        """Cancel a running record-with-trajectory session."""
        if self._record_traj_task is not None and not self._record_traj_task.done():
            self._record_traj_task.cancel()
            carb.log_info("[BLV] Record-with-trajectory cancelled by user.")
            self._widgets["rwt_status"].text = "Cancelled"
        # Ensure the data recorder is cleaned up even on cancel
        if self._data_recorder.is_setup:
            self._data_recorder.teardown()

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

        # Setup a fresh DataRecorder for this session
        recorder = DataRecorder(
            camera_path=self._camera_ctrl.camera_path,
            resolution=resolution,
            annotators=self._annotator_cfg,
        )

        self._widgets["rwt_status"].text = "Setting up writer..."
        try:
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
            carb.log_error(f"[BLV] record_with_trajectory error at frame {i}: {exc}")
            self._widgets["rwt_status"].text = f"Error at frame {i}: {exc}"
        finally:
            recorder.teardown()

        captured = recorder.frame_count
        self._widgets["rwt_status"].text = (
            f"Done — {captured} frames captured"
        )
        self._widgets["rwt_progress"].model.set_value(1.0)
        carb.log_info(
            f"[BLV] record_with_trajectory complete — {captured} frames → {output_dir}"
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

    def _scan_asset_folder(self, folder: str) -> None:
        """Scan an asset folder and update the browser."""
        cls = self._widgets["ab_class"].model.get_value_as_string().strip()
        target = self._widgets["ab_target"].model.get_value_as_string().strip()

        # Normalize path to resolve ~, double slashes, trailing slashes, etc.
        folder = os.path.normpath(os.path.expanduser(folder))

        if not folder or not os.path.isdir(folder):
            carb.log_warn(f"[BLV] Invalid asset folder: '{folder}'")
            self._widgets["ab_status"].text = "Error: invalid folder"
            return

        self._asset_browser.set_target_prim(target)
        count = self._asset_browser.set_folder(folder, class_name=cls)
        self._widgets["ab_status"].text = f"Found {count} USD files"
        self._widgets["ab_current"].text = "Current: None"

    def _on_next_asset(self) -> None:
        # Auto-scan if folder was changed
        folder = self._widgets["ab_folder"].model.get_value_as_string().strip()
        if folder and folder != self._asset_browser.asset_folder:
            self._scan_asset_folder(folder)

        self._sync_asset_browser_fields()
        success = self._asset_browser.next_asset()
        self._update_asset_browser_labels(success)

    def _on_prev_asset(self) -> None:
        folder = self._widgets["ab_folder"].model.get_value_as_string().strip()
        if folder and folder != self._asset_browser.asset_folder:
            self._scan_asset_folder(folder)

        self._sync_asset_browser_fields()
        success = self._asset_browser.previous_asset()
        self._update_asset_browser_labels(success)

    def _on_pick_target_prim(self) -> None:
        """Placeholder for a prim picker. For now, just sync the field."""
        target = self._widgets["ab_target"].model.get_value_as_string().strip()
        if target:
            self._asset_browser.set_target_prim(target)
            carb.log_info(f"[BLV] Target prim set to {target}")

    def _sync_asset_browser_fields(self) -> None:
        """Push current UI field values into the AssetBrowser instance."""
        cls = self._widgets["ab_class"].model.get_value_as_string().strip()
        target = self._widgets["ab_target"].model.get_value_as_string().strip()
        if cls:
            self._asset_browser.class_name = cls
        if target:
            self._asset_browser.target_prim_path = target

    def _update_asset_browser_labels(self, success: bool) -> None:
        idx = self._asset_browser.current_index
        total = self._asset_browser.total_assets
        name = self._asset_browser.current_asset_name
        self._widgets["ab_status"].text = f"Asset {idx + 1}/{total}"
        self._widgets["ab_current"].text = f"Current: {name}"

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
