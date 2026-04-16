# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An Omniverse Kit / Isaac Sim 5.1.0 extension (`blv.synth.data_collector`) for synthetic data collection in blind/low-vision (BLV) accessibility object detection research. It provides gamepad-driven FPS camera control, trajectory recording/playback, Replicator-based multi-modal data capture, and USD asset browsing with automatic semantic labeling.

## Development Environment

- **Platform**: Isaac Sim 5.1.0 / Kit 107.3.3
- **Python**: 3.11 (Isaac Sim's bundled interpreter)
- **GPU**: NVIDIA RTX (Replicator requires RTX for ray-traced captures)
- **Controller**: Logitech F710 (XInput) or any Xbox-compatible gamepad

There is no standalone build, lint, or test pipeline. The extension runs inside Isaac Sim and is loaded via the Extensions Manager (`Window > Extensions > "BLV Synth Data Collector"`). To iterate on code changes, modify files and reload the extension in Isaac Sim (or restart if `reloadable = false`).

## Architecture

### Module Dependency Graph

```
extension.py  (IExt lifecycle — on_startup / on_shutdown)
└── ui.py  (DataCollectorWindow — orchestrator, all UI, async workflows)
    ├── gamepad_camera.py  (GamepadCameraController — XInput FPS camera)
    ├── trajectory.py      (TrajectoryRecorder / TrajectoryPlayer / TrajectoryManager)
    ├── data_recorder.py   (DataRecorder — Replicator BasicWriter wrapper)
    ├── asset_browser.py   (AssetBrowser — USD reference swapping + semantic labels)
    └── location.py        (LocationManager — per-environment spawn point CRUD)
```

`ui.py` is the orchestrator — it owns instances of all backend modules, wires callbacks between them, and drives async capture workflows. The backend modules are decoupled from each other and from `omni.ui`.

### Key Design Decisions

- **Config priority**: `config/config.yaml` > `extension.toml` carb settings > hardcoded defaults. The UI reads YAML first, falls back to carb, then to hardcoded values. Runtime UI changes override all.
- **Persistent render product**: `DataRecorder` keeps the Replicator render product alive across recording sessions. Only the output directory is swapped via `reinitialize_writer()`. This avoids OmniGraph stale node handle errors that occur when destroying and recreating render products.
- **Auto-derived paths**: All filesystem paths derive from `{root_folder}/{class_name}/{environment}/[{location}/]`. Changing root, environment, class, or location in the UI triggers `_apply_project_paths()` which cascades to trajectory manager, data recorder, and asset browser.
- **Gamepad raw half-axis pattern**: `GamepadCameraController` stores each `GamepadInput` enum value independently in `raw_inputs` dict and computes signed axes per-frame. This prevents the accumulation bugs that plagued v1.
- **Camera transform ops**: The camera prim uses exactly 3 xformOps: `xformOp:translate`, `xformOp:rotateZ` (yaw), `xformOp:rotateX` (pitch + 90 offset). Isaac Sim uses Z-up coordinates.
- **Async capture**: `_record_with_trajectory_async()` is an `asyncio` coroutine that interleaves trajectory playback frames with `await DataRecorder.capture_frame()` calls. The UI remains responsive during capture.

### Data Flow: Record with Trajectory

1. `TrajectoryPlayer` loads JSON, sets camera pose via `GamepadCameraController.set_pose()` each frame
2. After each pose set, `await omni.kit.app.get_app().next_update_async()` lets the renderer catch up
3. `DataRecorder.capture_frame()` triggers Replicator to write annotator outputs (RGB, segmentation, bboxes)
4. Frame sampling parameter controls how many trajectory frames to skip between captures

### Output Directory Structure

```
{root_folder}/{class_name}/{environment}/
├── trajectories/                    # trajectory JSONs
├── captures/{class}_{asset}/{trajectory}/  # per-asset, per-trajectory captures
│   ├── rgb/
│   ├── semantic_segmentation/
│   └── bounding_box_2d_tight/
└── {location_name}/                 # optional location subdirectory
    ├── location.json                # spawn transform metadata
    └── trajectories/
```

### Configuration

Edit `config/config.yaml` for persistent defaults. Key sections: project paths, camera settings (speeds, focal length), rendering (resolution, RT subframes), annotator toggles. See the file for all options.

Extension-level defaults live in `config/extension.toml` under `[settings]`.
