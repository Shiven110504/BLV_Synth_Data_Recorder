# BLV Synth Data Collector v2

**Version**: 2.0.0  
**Platform**: Isaac Sim 5.1.0 / Kit 107.3.3  
**Controller**: Logitech F710 (XInput mode) or any Xbox-compatible gamepad  
**GPU**: NVIDIA RTX 5090 (recommended)

## Overview

Synthetic-data collection toolkit for blind/low-vision (BLV) accessibility object detection research. Provides gamepad-driven FPS camera control, trajectory recording/playback, Replicator-based multi-modal data capture, and USD asset browsing with automatic semantic labeling.

## What's New in v2

### Gamepad Fixes
1. **raw_inputs dict pattern** — Fixed axis accumulation bugs by storing each `GamepadInput` enum separately and computing signed axes per-frame.
2. **Correct control mapping** — Left stick forward/back now moves the camera forward/backward (was incorrectly mapped to elevation). Triggers control vertical movement.
3. **Pitch inversion fix** — Right stick up now correctly looks up (was inverted).
4. **No pitch clamp** — Removed the artificial 89° pitch limit to match Isaac Sim's default feel.
5. **Tuned speed defaults** — Move speed: 5.0 m/s (was 2.0), Look speed: 45.0 °/s (was 90.0), Speed step: 1.0 (was 0.5).

### UI Redesign
- **Single root configuration** — All paths auto-derive from a root folder + environment name.
- **Trajectory list** — Dropdowns of saved trajectories instead of manual file paths.
- **Annotator info panel** — Shows active annotators, resolution, and RT subframes.
- **Auto directory structure** — Captures organized by class, asset variant, and trajectory.

## Installation

1. Clone or copy the `blv.synth.data_collector` folder into your Isaac Sim extension search path.
2. In Isaac Sim, open **Window → Extensions** and search for "BLV Synth Data Collector".
3. Enable the extension. A window will appear docked in the Property panel.

## Quick Start

### 1. Project Settings
- Set the **Root Folder** (default: `~/blv_data`)
- Set the **Environment** name (e.g., `hospital_hallway`)
- Configure resolution (1280×720) and RT subframes (4)
- Click **Apply Settings**

### 2. Camera Control
- Enter your camera prim path (default: `/World/BLV_Camera`) and click **Set**
- Click **Enable Gamepad** to activate FPS-style control:
  - **Left Stick**: Move forward/back, strafe left/right
  - **Right Stick**: Look (yaw/pitch)
  - **Right Trigger**: Move up
  - **Left Trigger**: Move down
  - **D-Pad Up/Down**: Increase/decrease speed
  - **Left Bumper**: Toggle slow mode (25% speed)

### 3. Record a Trajectory
- Enter a trajectory name (e.g., `trajectory_001`)
- Click **Record**, fly the camera path, then click **Stop & Save**
- The trajectory JSON is saved to `{root}/{environment}/trajectories/`

### 4. Playback
- Select a trajectory from the dropdown
- Click **Play** to replay frame-by-frame

### 5. Data Capture
- Click **Setup Writer** to initialize the Replicator pipeline
- The writer captures: RGB, semantic segmentation, colorized semantic segmentation, bounding box 2D tight

### 6. Record with Trajectory
- Select a trajectory from the dropdown
- Click **Record Trajectory** — the camera replays the path while capturing data at every frame
- Output goes to `{root}/{environment}/captures/{class}_{asset}/{trajectory}/`

### 7. Asset Browser
- Set the asset folder path, class name, and target prim
- Use **Prev/Next** to cycle through USD files
- Each asset is loaded as a USD reference with automatic semantic labeling

## Directory Structure

```
{root_folder}/
├── {environment}/
│   ├── trajectories/
│   │   ├── trajectory_001.json
│   │   ├── trajectory_002.json
│   │   └── ...
│   └── captures/
│       ├── {class_name}_{asset_variant}/
│       │   ├── trajectory_001/
│       │   │   ├── rgb/
│       │   │   ├── semantic_segmentation/
│       │   │   ├── bounding_box_2d_tight/
│       │   │   └── colorize_semantic_segmentation/
│       │   └── ...
│       └── ...
```

## Gamepad Control Map (Logitech F710 / Xbox)

| Input              | Action                     |
|--------------------|----------------------------|
| Left Stick Up/Down | Move forward/backward      |
| Left Stick L/R     | Strafe left/right          |
| Right Stick Up/Down| Pitch (look up/down)       |
| Right Stick L/R    | Yaw (look left/right)      |
| Right Trigger      | Move up (vertical)         |
| Left Trigger       | Move down (vertical)       |
| D-Pad Up           | Increase move speed (+1.0) |
| D-Pad Down         | Decrease move speed (-1.0) |
| Left Bumper        | Toggle slow mode (25%)     |

## Configuration

Extension defaults can be overridden in `extension.toml` or via Carb settings:

| Setting                              | Default            | Description              |
|--------------------------------------|--------------------|--------------------------|
| `default_camera_path`                | `/World/BLV_Camera`| Camera prim path         |
| `default_move_speed`                 | `5.0`              | Move speed (m/s)         |
| `default_look_speed`                 | `45.0`             | Look speed (°/s)         |
| `default_resolution_width`           | `1280`             | Capture width (px)       |
| `default_resolution_height`          | `720`              | Capture height (px)      |
| `default_rt_subframes`               | `4`                | RT subframes per capture |
| `default_root_folder`                | `~/blv_data`       | Project root folder      |
| `default_environment`                | `hospital_hallway` | Environment name         |

## Trajectory JSON Format

```json
{
  "version": "1.0",
  "name": "trajectory_001",
  "environment": "hospital_hallway",
  "camera_path": "/World/BLV_Camera",
  "fps": 60,
  "frame_count": 300,
  "created": "2026-04-06T13:00:00",
  "frames": [
    {"frame": 0, "position": [1.0, 2.0, -3.0], "rotation": [10.0, -45.0, 0.0]},
    ...
  ]
}
```

## Dependencies

- `omni.kit.uiapp`
- `omni.ui`
- `omni.usd`
- `omni.kit.viewport.utility`
- `omni.replicator.core`
- `isaacsim.core.api`
- `isaacsim.core.utils`

## Author

Shiven Patel
