# BLV Synth Data Collector — Bug Bash & Senior-Engineer Code Review

**Target:** `blv.synth.data_collector` v2.0.0 for Isaac Sim 5.1.0 / Kit 107.3.3
**Scope:** full repo survey against Isaac Sim 5.1.0 + OpenUSD 25.05 docs and local
`isaacsim.core.utils.*` sources
**Date:** 2026-04-21
**Reviewer perspective:** senior software engineer specialized in Omniverse Kit
extensions, USD, and Replicator-based synthetic data pipelines.

This is an evidence-driven review, not a checklist. Every finding cites file +
line + function, names the research basis (Isaac Sim / USD docs, bundled source),
explains *why* the code is problematic, and suggests a concrete remediation
direction. I have **not** modified any source — this document is the deliverable.

---

## Summary table

| # | Severity | File | Function / Area | One-line |
|---|----------|------|-----------------|----------|
| 1 | CRITICAL | `backend/gamepad_camera.py` | `_ensure_camera_prim`, `focal_length` setter | USD `focalLength` is in *tenths of a scene unit*, not mm — current code writes mm directly. |
| 2 | CRITICAL | `backend/stage.py` | `_on_stage_event` | External stage opens (File → Open / `open_stage`) fire CLOSING but *do not* run pre-close hooks — render product leaks onto dead stage. |
| 3 | CRITICAL | `backend/asset_browser.py` | `capture_transform_from_prim` vs `load_asset` | Reads **world** transform, reapplies it as **local** under `parent_prim_path` — silent miscompose when parent has non-identity transform. |
| 4 | HIGH | `backend/gamepad_camera.py` | `_on_update` | No pitch clamp — right-stick up past ±90° flips the scene upside-down (gimbal flip). |
| 5 | HIGH | `backend/session.py` | `_current_run_name`, `capture_output_dir` | Re-running capture against the same asset/trajectory silently overwrites previous frames. No uniqueness / no run index. |
| 6 | HIGH | `backend/asset_browser.py` | `_apply_semantic_label` | Label applied only to the outer Xform that *references* the USD. Relies on LabelsAPI inheritance — brittle when referenced asset has its own `Semantics`/`LabelsAPI` authored. |
| 7 | HIGH | `backend/trajectory.py` | `TrajectoryRecorder._on_update`, `TrajectoryPlayer._on_update` | Record & playback are per-tick, so frame rate and trajectory speed are locked to Kit's variable tick. Trajectories do not replay at the rate they were recorded at. |
| 8 | HIGH | `backend/trajectory_io.py` | `build_trajectory_payload` | Hardcoded `fps=60` written into trajectory JSON regardless of actual recording rate. |
| 9 | HIGH | `backend/config.py` | `_carb_float`, `_carb_int` | `return val or None` collapses legitimate `0.0` / `0` carb settings to "unset". |
| 10 | HIGH | `backend/gamepad_camera.py` | `__init__` | `move_speed or settings.get_as_float(...) or DEFAULT` — explicit `0.0` silently falls through to the default. |
| 11 | MEDIUM | `backend/gamepad_camera.py` | `_on_gamepad_event` | Radial dead zone is **clipped** not **remapped** — stick output jumps from 0 to 0.15× at the boundary. |
| 12 | MEDIUM | `backend/gamepad_camera.py` | `_apply_pose_to_usd` | No yaw/pitch wrap — angles drift unbounded, float precision degrades over a long session. |
| 13 | MEDIUM | `backend/gamepad_camera.py` | module-level | Hard-coded Z-up basis (`forward = (-sin, cos, 0)`). Breaks on stages authored with `upAxis=Y`. |
| 14 | MEDIUM | `backend/location.py` | `save_transform`, `create_location` | Non-atomic JSON writes. Crash mid-write leaves a truncated/empty `location.json`, which the loader then treats as corrupt. |
| 15 | MEDIUM | `backend/trajectory_io.py` | `write_trajectory_json` | Same non-atomic write pattern; a long trajectory written mid-shutdown can be corrupted. |
| 16 | MEDIUM | `backend/session.py` | `collect_all`, `record_all_trajectories`, `record_with_trajectory` | `asyncio.CancelledError` is raised *through* `record_with_trajectory` but caught only in the UI section; the in-flight Replicator orchestrator is not drained before the next task starts. |
| 17 | MEDIUM | `backend/capture.py` | `capture_frame` | Swallows Replicator exceptions silently (logs + continues). A failed `step_async` still increments `_frame_count`, inflating counters and producing gaps in output numbering. |
| 18 | MEDIUM | `backend/session.py` | `_on_asset_moved` | Auto-saves the spawn transform *on every tick the user nudges the prim* (the `_AUTOSAVE_DEBOUNCE_TICKS=0` comment acknowledges this). Each change is a full JSON rewrite with no debounce. |
| 19 | MEDIUM | `backend/asset_browser.py` | `capture_transform_from_prim` | Uses `Gf.Transform()` which factors rotation out of a matrix that may also contain shear from non-uniform scale — quaternion round-trip is lossy. |
| 20 | MEDIUM | `backend/asset_browser.py` | `load_asset` | Asset stem collisions (two assets with same `os.path.splitext(basename)`) produce different prim paths via `get_stage_next_free_path`, but **captures** key off the stem alone → captures from different assets land in the same run folder. |
| 21 | LOW | `backend/gamepad_camera.py` | `_on_update` | Reads `dt` from Kit payload without clamping. One bad frame (alt-tab, scene swap) produces a `dt` of several seconds and the camera teleports. |
| 22 | LOW | `backend/stage.py` | `switch_to` | `_WARMUP_FRAMES=20` is an arbitrary empirical constant. A busy GPU may still not have Hydra ready after 20 ticks. Prefer an event-driven wait on `STAGE_EVENT_HYDRA_GEOSTREAMING_STARTED` or a settled-texture check. |
| 23 | LOW | `backend/asset_browser.py` | `_sanitize_prim_name` | Strips everything non-`[A-Za-z0-9_]`, but doesn't guard against collisions with reserved USD names or check against existing siblings before calling `get_stage_next_free_path`. |
| 24 | LOW | `backend/capture.py` | `DEFAULT_ANNOTATORS` / `_build_writer` | `colorize_semantic_segmentation` is a *modifier* to `semantic_segmentation`, not an independent annotator — BasicWriter only respects it when `semantic_segmentation=True`. UI exposes it as a standalone toggle, which can confuse users. |
| 25 | LOW | `ui/sections/record_with_trajectory.py` | `_on_record_all`, `_on_record` | Stores `self._task` but `_on_record_all` overwrites it without checking `self._task.done()` — launching both in quick succession orphans the first coroutine. |
| 26 | LOW | `cli/bootstrap.py` (pattern) | — | `signal.signal(SIGINT, ...)` before/after Kit boot can conflict with Kit's own signal handling and with asyncio on 3.11. Prefer `loop.add_signal_handler`. *(Not re-read in this pass — flagged from earlier survey.)* |

---

## Research basis

Before diving into the findings, the sources I cross-referenced:

### Isaac Sim 5.1.0

- `isaacsim.core.utils.xforms.reset_and_set_xform_ops(prim, translation, orientation, scale)` — verified against local source at
  `/home/shiven/miniconda3/envs/isaac5/lib/python3.11/site-packages/isaacsim/core/utils/xforms.py`.
  Contract: clears op order, creates exactly **three** ops in the order
  `[xformOp:translate (PrecisionDouble), xformOp:orient (PrecisionDouble),
  xformOp:scale (PrecisionDouble)]`.
- `isaacsim.core.utils.semantics.add_labels(prim, labels, instance_name="class", overwrite=True)` —
  `overwrite=True` maps to `mode="replace"`. Backs onto `UsdSemantics.LabelsAPI`
  (the Kit 107.3 / USD 25.05 replacement for the old `Semantics` schema).
- Replicator BasicWriter / `rep.orchestrator.step_async(rt_subframes=, delta_time=, pause_timeline=)`
  and `rep.orchestrator.set_capture_on_play(False)` — Omniverse Replicator docs.
- Hydra `render_product.hydra_texture.set_updates_enabled(False)` before `render_product.destroy()` —
  recommended teardown sequence documented in the Replicator "stage swap" notes.

### OpenUSD 25.05

- `UsdGeomCamera.GetFocalLengthAttr()` — **"Focal length, in tenths of a scene
  unit (see UsdGeomLinearUnits)."** Authored value is divided by `metersPerUnit`
  conversion factor chosen by the application to produce a physical mm value.
  *This is the single biggest pitfall in the repo — see Finding 1.*
- `xformOp:orient` expects `Gf.Quatf` or `Gf.Quatd`; 4-float constructor order
  is `(real, i, j, k)`.
- `UsdGeomXformable.ComputeLocalToWorldTransform()` — matrix is in stage
  units, includes all ancestor ops.

---

# Findings (detailed)

---

## Finding 1 — CRITICAL — USD focal length units

**File:** `blv/synth/data_collector/backend/gamepad_camera.py`
**Lines:** 230-242 (setter), 288-292 (`_ensure_camera_prim`)
**Functions:** `focal_length.setter`, `_ensure_camera_prim`
**Config:** `config/config.yaml` line "focal_length: 28.0   # in mm — USD uses
'tenths of a scene unit'; see camera setup" — the author **knew** about the
unit mismatch but the code still writes the mm value unchanged.

### Bug

```python
# gamepad_camera.py:238
UsdGeom.Camera(prim).GetFocalLengthAttr().Set(float(val))
# gamepad_camera.py:290
cam.GetFocalLengthAttr().Set(float(self._focal_length))
```

`UsdGeomCamera::focalLength` is **not** in millimeters. OpenUSD 25.05 specifies
it as *"Focal length, in tenths of a scene unit"*. For a stage authored with
`metersPerUnit = 0.01` (cm, the Kit/Isaac Sim default), 1 scene unit = 1 cm, so:

- Desired physical 28 mm focal length → 2.8 cm → USD value must be **28**
  (2.8 × 10 = 28, coincidentally correct for cm stages).
- Desired physical 28 mm focal length on a **meters** stage (`metersPerUnit =
  1.0`) → 0.028 m → USD value must be **0.28**.
- Current code writes `28.0` regardless of `metersPerUnit`. On a meters stage
  the effective focal length is 2.8 m — an ultra-telephoto lens with a pinhole-
  sized field of view.

The class already reads `metersPerUnit` (line 274-276: `self._units_per_meter =
1.0 / mpu`) for translation, but doesn't apply the conversion to focal length.

### Why this matters

- Captures from a meters-unit stage are rendered with a wildly wrong field of
  view; bounding boxes & segmentation masks are still correct internally but
  the RGB images are unusable as training data.
- Silent — the image looks "zoomed in" but not obviously broken.
- Horizontal/vertical aperture defaults (24.0, 16.0 — the USD DPX defaults) are
  also never written, so the user's FOV is implicitly an APS-C sensor. For a
  dataset this needs to be pinned explicitly.

### Suggested fix

```python
def _write_focal_length(self, cam, focal_mm: float) -> None:
    stage = cam.GetPrim().GetStage()
    mpu = UsdGeom.GetStageMetersPerUnit(stage) or 0.01
    # focal_mm is millimeters; USD stores (mm / 1000 / mpu) * 10.
    scene_units = (focal_mm / 1000.0) / mpu
    cam.GetFocalLengthAttr().Set(float(scene_units * 10.0))
```

Also pin `horizontalAperture` / `verticalAperture` so FOV is deterministic
across runs. If you want proper physical-camera behaviour in Replicator, also
set `clippingRange`, `focusDistance`, `fStop`.

---

## Finding 2 — CRITICAL — External stage opens bypass pre-close hooks

**File:** `blv/synth/data_collector/backend/stage.py`
**Lines:** 238-262
**Function:** `StageController._on_stage_event`

### Bug

```python
def _on_stage_event(self, evt) -> None:
    ...
    if event_type == int(StageEventType.CLOSING):
        if not self._is_switching:
            self._bus.emit("stage_will_close")
    elif event_type == int(StageEventType.OPENED):
        if not self._is_switching:
            self._bus.emit("stage_opened")
```

For externally-initiated stage changes (user clicks File → Open, or another
extension calls `open_stage`), the controller only *emits bus events*. It does
**not** run the pre-close hooks that:

1. Stop the trajectory player subscription.
2. Stop the trajectory recorder subscription.
3. Disable the gamepad update subscription.
4. Clear the `AssetBrowser`'s cached prim paths.
5. **Tear down the Replicator render product** (`prepare_for_stage_change_async`).

Consequence: step 5 is the killer. The render product's hydra texture holds
a reference into the now-destroyed stage's `UsdImagingDelegate`. On the next
orchestrator tick (or on the next capture after the user opens a new stage),
Kit crashes with *"accessed invalid null prim"* or leaks OmniGraph nodes.

### Why this matters

- The entire tear-down sequence documented in the `stage.py` module docstring
  exists *precisely* to handle this — but only the `switch_to()` code path
  runs it. Any external stage change is unguarded.
- The user has no way of knowing they shouldn't use File → Open while the
  extension is loaded.

### Suggested fix

Two options, pick one:

1. **Detect external closes and run the teardown inline.** Requires running
   the pre-close hooks synchronously (or scheduling them on the running loop)
   from `_on_stage_event` when `event_type == CLOSING and not _is_switching`.
   Awaitable hooks complicate this — you'd need a sync fallback path.
2. **Route ALL stage opens through `switch_to`.** Disable Kit's default
   `File → Open` when the extension is active, or wrap it with a confirmation
   dialog that calls `switch_to(path)` instead. This is the cleaner path —
   centralises the entire stage lifecycle on one code path.

Either way, also emit `"hook_error"` loudly so the UI can surface the problem
if the fallback isn't perfect.

---

## Finding 3 — CRITICAL — World-vs-local transform mix-up

**File:** `blv/synth/data_collector/backend/asset_browser.py`
**Lines:** 223-243 (`capture_transform_from_prim`), 447-459 (`_read_world_transform`), 272-375 (`load_asset`), 350-355 (`reset_and_set_xform_ops` call)
**Functions:** `capture_transform_from_prim`, `load_asset`

### Bug

`capture_transform_from_prim` reads the **world-space** transform:

```python
# asset_browser.py:451-458
xformable = UsdGeom.Xformable(prim)
mat = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
xform = Gf.Transform()
xform.SetMatrix(mat)
translate = Gf.Vec3d(xform.GetTranslation())
orient    = Gf.Quatd(xform.GetRotation().GetQuat())
scale     = Gf.Vec3d(xform.GetScale())
```

Stores that in `self._spawn_translate/_orient/_scale`.

Then `load_asset` applies those values as **local** ops on a new prim that is
created under `self._parent_prim_path` (default `/World`, configurable):

```python
# asset_browser.py:350-355
reset_and_set_xform_ops(
    prim,
    self._spawn_translate,
    self._spawn_orient,
    self._spawn_scale,
)
```

`reset_and_set_xform_ops` only touches the prim's local op stack — it does not
compensate for ancestor transforms. So if the parent has any non-identity
transform (rotation on `/World`, scale on a parent Xform, an environment USD
that nests assets deeper), the spawned asset lands in the *wrong world pose*.

### Why this matters

- `location.json` persists the captured "spawn transform" and re-applies it on
  the next run — any mismatch is *permanent* until edited by hand.
- `collect_all` explicitly loads location.json and re-applies the same numbers
  (`session.py:688-692`), so every asset at that location inherits the bug.
- Only discovered when users scan an environment USD that has `/World`
  rotated 90° from the authoring app's default — a very common case when
  importing `.usdz` from iOS LiDAR captures, for example.

### Suggested fix

Decide on a single space and keep it throughout:

1. **Option A (preferred):** Always work in the parent's **local** space.
   In `capture_transform_from_prim`, compute `parent_local = parent.
   ComputeLocalToWorldTransform().GetInverse() * prim.ComputeLocalToWorld()`,
   then factor that matrix into T/R/S. `reset_and_set_xform_ops` then re-
   applies it against the same parent, and you get pixel-identical world
   poses.
2. **Option B:** Work in world space both ways — but `reset_and_set_xform_ops`
   writes to the prim's local ops, not world, so you'd need to invert the
   parent transform at load time. More error-prone.

Either way, add a unit test with a non-identity `/World` transform.

---

## Finding 4 — HIGH — No pitch clamp, camera gimbal-flips

**File:** `blv/synth/data_collector/backend/gamepad_camera.py`
**Lines:** 405-407 (accumulate), 439 (write)
**Function:** `_on_update`

### Bug

```python
self._pitch += pitch_in * look * dt
...
pitch_attr.Set(float(90.0 + self._pitch))
```

`self._pitch` is unbounded. Holding the right stick up runs `_pitch` past
+90° — at which point `rotateX(90 + 95) = rotateX(185)` rolls the world
upside-down, and the user's yaw is now inverted because the camera basis
has flipped.

### Why this matters

This is the classic FPS gimbal-flip bug. Unrecoverable in-world (the user
has to push the stick back past the singularity with an inverted control
scheme). Trajectories recorded during a flipped state produce playback
frames where every subsequent yaw input is mirrored.

### Suggested fix

Clamp at `±89.0°` (give yourself 1° of safety margin):

```python
self._pitch = max(-89.0, min(89.0, self._pitch + pitch_in * look * dt))
```

Also guard `set_pose()` (line 257-261) so loaded trajectories can't
introduce an unclamped pitch through the backdoor.

---

## Finding 5 — HIGH — Captures overwrite on re-run

**File:** `blv/synth/data_collector/backend/session.py`
**Lines:** 430-443 (`_current_run_name`, `capture_output_dir`)
**Function:** `_current_run_name`

### Bug

```python
def _current_run_name(self) -> str:
    stem = self.assets.current_asset_stem
    if stem:
        return _paths.sanitize_folder_name(stem)
    ...
```

Running `record_with_trajectory("traj_001.json", ...)` for the same asset
and the same trajectory twice produces identical output paths:

```
{root}/{class}/{env}/{loc}/{asset_stem}/traj_001/rgb/rgb_000000.png
                                                   rgb_000001.png
                                                   ...
```

BasicWriter starts its internal counter at 0 for each `writer.initialize(...)`
call, so the second run *overwrites* the first file-by-file. (Current
`reinitialize_writer` resets `_frame_count = 0` at line 280, confirming the
overwrite behaviour.)

### Why this matters

- A failed capture you retry replaces the successful earlier capture you
  forgot about.
- There is no on-disk marker (no run index, no timestamp subdirectory,
  no `run_01/`, `run_02/`) to prevent this. The user has no way to tell
  two sessions apart unless they moved the output folder manually.
- For a dataset being collected over weeks, this is a silent data-loss
  bug.

### Suggested fix

Either:

1. **Timestamped subdirectory** — `capture_output_dir` returns
   `{base}/{asset}/{traj}/{YYYY-MM-DD_HHMMSS}/` — cheap, guaranteed unique,
   no collision detection needed.
2. **Monotonic run index** — scan the target dir, pick the next unused
   `run_NN`. Slightly more complex, yields stable short names.
3. **Refuse to run** if the target dir exists and is non-empty; force the
   user to pass `--overwrite` or clean up manually.

Option 1 is what I'd pick for a research dataset — the timestamp doubles
as provenance.

---

## Finding 6 — HIGH — Semantic label brittle under inherited LabelsAPI

**File:** `blv/synth/data_collector/backend/asset_browser.py`
**Lines:** 487-525
**Function:** `_apply_semantic_label`

### Bug

```python
if self._class_name:
    add_labels(target_prim, labels=[self._class_name], instance_name="class")
```

`target_prim` is the outer `Xform` that holds the `AddReference` arc
(`load_asset` created it at line 328-333). The referenced USD's *root* prim
may *already* have its own `UsdSemantics.LabelsAPI` authored with a different
class — when Kit Replicator traverses for `semantic_segmentation` /
`bounding_box_2d_tight`, the inner authored label wins at the leaf prims
where geometry actually lives, because LabelsAPI is not strictly
inheritance-only; authored values at nested prims take precedence.

### Why this matters

- Users importing third-party assets (Nvidia Sample Assets, Omniverse
  Content, or `.usdz` captures) frequently hit pre-labelled content.
- Symptom: semantic segmentation appears to be labelled as the asset's
  original class, not the user's `class_name`, but the outer prim looks
  "correct" in the USD tree.
- The `stale_paths` cleanup loop (line 499-508) removes labels from the
  *parent Xforms of previous assets*, not from the geometry prims inside
  the references. A rebuilt scene can carry persistent labels on deep
  prims that the cleanup can't see.

### Suggested fix

Two-step approach:

1. Walk `target_prim.GetAllDescendants()` (post-reference, after the stage
   has been asked to compose) and `remove_labels(descendant, instance_name=
   "class")` on any prim that has `LabelsAPI` authored.
2. Then `add_labels` on the outer prim. Isaac Sim Replicator's label
   resolution walks upward — clearing the overrides at descendants restores
   proper inheritance.

Alternatively, set the label on the **asset USD's default prim** at
reference-time via a `PrimSpec` override in the session layer, not by
stomping on the referenced asset.

---

## Finding 7 — HIGH — Trajectories are tick-rate dependent

**File:** `blv/synth/data_collector/backend/trajectory.py`
**Lines:** 109-118 (`TrajectoryRecorder._on_update`), 227-253 (`TrajectoryPlayer._on_update`)
**Functions:** `TrajectoryRecorder._on_update`, `TrajectoryPlayer._on_update`

### Bug

Both recorder and player operate per-Kit-tick:

- Recorder samples one pose per tick. If Kit runs at 60 fps most of the time
  but drops to 15 fps during heavy scene ops, the resulting trajectory has
  unevenly-spaced samples but no timing information.
- Player advances one frame per tick (`self._current_frame += 1`). So a
  trajectory recorded at 60 fps plays back at *whatever the current tick
  rate is* — on an overloaded GPU this is slower, on a lightweight scene
  it's faster.

The JSON also hard-codes `fps: 60` (see Finding 8) so downstream consumers
cannot reconstruct correct timing either.

### Why this matters

- The whole point of "record a trajectory once, replay against every asset"
  is deterministic re-capture. If playback speed varies by load, the
  sampled frames differ between runs.
- `record_with_trajectory` samples *every Nth frame* (`frame_step`) of the
  trajectory, not every Nth second. Two captures of the same trajectory at
  different Kit load → different scenes viewed.

### Suggested fix

Record **timestamps** per frame:

```python
self._frames.append({
    "frame": self._frame_count,
    "t": event.payload["absolute_time"],   # or time.monotonic()
    "position": pose["position"],
    "rotation": pose["rotation"],
})
```

Then in playback:

- Either advance by real-time (interpolate between samples at the target
  dt), or
- Sample the trajectory by target time-stamps rather than by `_current_
  frame` integer indexing.

Also capture `omni.timeline` FPS at record start so the JSON's `fps` field
is truthful (Finding 8).

---

## Finding 8 — HIGH — Hardcoded fps in trajectory schema

**File:** `blv/synth/data_collector/backend/trajectory_io.py`
**Line:** 56 (default), 70 (written)
**Function:** `build_trajectory_payload`

### Bug

```python
def build_trajectory_payload(..., fps: int = 60, created: str = "") -> Dict[str, Any]:
    return {
        ...
        "fps": fps,
        ...
    }
```

No caller ever passes `fps` — it's always 60. The `TrajectoryRecorder` does
not capture the actual Kit tick rate at record start, so the field cannot
reflect reality even if you wanted it to.

### Why this matters

Downstream tools (annotator post-processing, ROSbag export, video
generation) rely on the `fps` field to time-align RGB / depth / IMU.
A lie in the schema is a silent inaccuracy in every dataset derived
from this extension.

### Suggested fix

At record-start:

```python
rate_hz = carb.settings.get_settings().get_as_float(
    "/app/runLoops/main/rateLimitFrequency"
) or 60.0
self._fps = rate_hz
```

Then propagate into `build_trajectory_payload(fps=self._fps)`.
Combine with the per-frame timestamps from Finding 7 and the resulting
JSON is both self-describing and actually replayable.

---

## Finding 9 — HIGH — `val or None` collapses legitimate zeros

**File:** `blv/synth/data_collector/backend/config.py`
**Lines:** 144-152 (`_carb_int`), 155-163 (`_carb_float`)
**Functions:** `_carb_int`, `_carb_float`

### Bug

```python
def _carb_float(ext_key, setting_key):
    ...
    val = settings.get_as_float(...)
    return val or None
```

`0.0 or None` evaluates to `None`. So any user who sets `default_rt_subframes
= 0` (valid — "raster-only, no RT subframes") or `default_move_speed = 0` (to
disable gamepad translation) gets the hardcoded default instead.

### Why this matters

- Silent override. The UI shows the default value without telling the user
  their setting was ignored.
- `rt_subframes = 0` specifically *is* something Replicator supports for
  fast debug captures without path tracing accumulation.

### Suggested fix

Use a three-way sentinel:

```python
def _carb_float(ext_key, setting_key) -> Optional[float]:
    if carb is None:
        return None
    settings = carb.settings.get_settings()
    key = f"/{ext_key}/{setting_key}"
    try:
        # Detect "key exists" vs "key missing" — carb returns a default
        # value type for missing keys.  Introspect via `get` first.
        if not settings.is_setting(key):  # pseudocode; real API differs
            return None
        return float(settings.get_as_float(key))
    except Exception:
        return None
```

Or bind carb settings through a typed schema (pydantic / dataclass) that
distinguishes absent from zero.

---

## Finding 10 — HIGH — `__init__` fallthrough on explicit 0

**File:** `blv/synth/data_collector/backend/gamepad_camera.py`
**Lines:** 73-82
**Function:** `GamepadCameraController.__init__`

### Bug

```python
self._move_speed: float = (
    move_speed
    or settings.get_as_float(f"/{_ext}/default_move_speed")
    or self.DEFAULT_MOVE_SPEED
)
```

Same pattern as Finding 9, one layer up. `move_speed=0.0` (valid — "don't
translate on stick input") falls through the `or` chain.

### Why this matters

Same as Finding 9. Caller-supplied zero is silently replaced with the
default.

### Suggested fix

```python
self._move_speed: float = (
    move_speed
    if move_speed is not None
    else _resolve_setting(
        f"/{_ext}/default_move_speed", self.DEFAULT_MOVE_SPEED
    )
)
```

Same pattern for `_look_speed`. Applies to any `Optional[float]` path in
the codebase.

---

## Finding 11 — MEDIUM — Dead zone clips instead of remapping

**File:** `blv/synth/data_collector/backend/gamepad_camera.py`
**Lines:** 351-352
**Function:** `_on_gamepad_event`

### Bug

```python
if abs(val) < self.DEAD_ZONE:
    val = 0.0
```

XInput raw values are in `[0, 1]`. Below 0.15 we write 0; at 0.15 we write
0.15. The output jumps from 0 to 0.15 at the dead-zone boundary — a
discontinuous control response. Users perceive this as a "sticky" stick
that suddenly kicks into motion.

### Why this matters

- Perceived control quality. Not a correctness bug per se, but the
  classical remap is a 5-line fix and it's weird that a senior-grade
  FPS controller doesn't do it.

### Suggested fix

Remap so the effective stick output is continuous:

```python
if abs(val) < self.DEAD_ZONE:
    val = 0.0
else:
    sign = 1.0 if val > 0 else -1.0
    val = sign * (abs(val) - self.DEAD_ZONE) / (1.0 - self.DEAD_ZONE)
```

---

## Finding 12 — MEDIUM — Yaw/pitch drift unbounded

**File:** `blv/synth/data_collector/backend/gamepad_camera.py`
**Lines:** 406-407, 437-439
**Function:** `_on_update`, `_apply_pose_to_usd`

### Bug

Nothing wraps `self._yaw` into `[-180, 180]`. A player who holds the look
stick for 10 minutes accumulates `_yaw = -4800.0` or similar. `math.sin`
/ `math.cos` still return the right basis vectors (trigonometry is
periodic), but the USD-stored value in `xformOp:rotateZ` is a 4-digit
number that grows forever.

### Why this matters

- `Gf.Quatd` quantization error compounds when `xformOp:orient` is later
  derived from this.
- Some viewport tools clamp `rotateZ` display to `[0, 360)` and render
  garbage when given `-4800`.
- Trajectory JSONs grow because `rotation: [pitch, -4823.1, 0]` is longer
  than `rotation: [pitch, 176.9, 0]`.

### Suggested fix

```python
self._yaw = ((self._yaw + 180.0) % 360.0) - 180.0
```

Once per `_on_update`, after the accumulate.

---

## Finding 13 — MEDIUM — Z-up assumption hard-coded

**File:** `blv/synth/data_collector/backend/gamepad_camera.py`
**Lines:** 412-414
**Function:** `_on_update`

### Bug

```python
forward = Gf.Vec3d(-math.sin(yaw_rad), math.cos(yaw_rad), 0.0)
right   = Gf.Vec3d( math.cos(yaw_rad), math.sin(yaw_rad), 0.0)
up      = Gf.Vec3d(0.0, 0.0, 1.0)
```

Baked assumption: `upAxis=Z`. The docstring announces "Isaac Sim uses
Z-up" — true, for Isaac Sim's *default* stages. USD stages authored in
Maya (Y-up) retain `upAxis=Y`. When loaded into Isaac Sim they compose
fine but your forward vector is now pointing sideways.

### Why this matters

- `collect_all` opens whatever USD file the user points it at. Maya /
  Blender / Houdini USD exports are Y-up by convention.
- No detection → no warning → captures are taken with the camera pointing
  90° off.

### Suggested fix

Read `UsdGeom.GetStageUpAxis(stage)` on setup and pick basis accordingly:

```python
up_axis = UsdGeom.GetStageUpAxis(stage)
if up_axis == UsdGeom.Tokens.z:
    forward = Gf.Vec3d(-math.sin(yaw_rad), math.cos(yaw_rad), 0.0)
    up = Gf.Vec3d(0.0, 0.0, 1.0)
elif up_axis == UsdGeom.Tokens.y:
    forward = Gf.Vec3d(math.sin(yaw_rad), 0.0, -math.cos(yaw_rad))
    up = Gf.Vec3d(0.0, 1.0, 0.0)
```

(Also applies to the `xformOp:rotate*` ordering — on Y-up stages you want
`rotateY` for yaw and the pitch-offset base is different.)

---

## Finding 14 — MEDIUM — Non-atomic JSON writes in LocationManager

**File:** `blv/synth/data_collector/backend/location.py`
**Lines:** 161-167 (`create_location`), 188-204 (`save_transform`)
**Functions:** `create_location`, `save_transform`

### Bug

```python
with open(filepath, "w") as fh:
    json.dump(data, fh, indent=2)
```

If the process is killed mid-write (Kit crash, Ctrl-C during `collect_all`,
OOM, power loss), `location.json` can be left truncated or empty.
`save_transform` at line 190-193 catches `JSONDecodeError` by rebuilding
from scratch — which then *silently overwrites the corrupted file with
whatever transform is in memory*, losing any fields the user had edited
by hand that weren't in the in-memory dict.

### Why this matters

- `_on_asset_moved` (`session.py:399-416`) auto-saves on every tick with
  transform changes. The write-corruption window is large and frequent.
- The rebuild-on-corruption path at `save_transform` hides the problem
  from the user — they only notice custom fields have been wiped.

### Suggested fix

Standard write-then-rename pattern:

```python
import tempfile, os
tmp = tempfile.NamedTemporaryFile(
    "w", dir=os.path.dirname(filepath), delete=False, suffix=".tmp")
try:
    json.dump(data, tmp, indent=2)
    tmp.flush()
    os.fsync(tmp.fileno())
    tmp.close()
    os.replace(tmp.name, filepath)
except Exception:
    os.unlink(tmp.name)
    raise
```

Apply the same pattern to `trajectory_io.write_trajectory_json` (Finding 15).

---

## Finding 15 — MEDIUM — Non-atomic trajectory write

**File:** `blv/synth/data_collector/backend/trajectory_io.py`
**Lines:** 87-95
**Function:** `write_trajectory_json`

### Bug

Same pattern as Finding 14. A 10-minute trajectory is several hundred KB of
JSON; interrupt at the wrong moment and you lose the whole recording.

### Suggested fix

Same write-then-rename pattern. Trajectories are small enough that
`json.dump` → `os.replace` is cheap.

---

## Finding 16 — MEDIUM — CancelledError doesn't drain the orchestrator

**File:** `blv/synth/data_collector/backend/session.py`
**Lines:** 503-515 (`record_with_trajectory`), 589-606 (`record_all_trajectories`), 739-763 (`collect_all`)
**Function:** `record_with_trajectory`, `record_all_trajectories`, `collect_all`

### Bug

When the user clicks Cancel, the UI sections call `self._task.cancel()`
(e.g. `ui/sections/record_with_trajectory.py:124`). That propagates as a
`CancelledError` at the next `await`. But the in-flight `await
self.recorder.capture_frame()` which itself awaits `rep.orchestrator.
step_async(...)` does not wait for the Replicator orchestrator to finish
draining — it simply unwinds. The next capture workflow inherits a busy
orchestrator and starts writing frames over the cancelled run.

### Why this matters

- `collect_all` does `self.recorder.teardown()` at the top (line 627) to
  work around this symptom. That's a treat-the-symptom workaround, not a
  fix, and it doesn't help `record_with_trajectory` or `record_all_
  trajectories` which *also* call `teardown()` at the top (line 468 and
  533) for the same reason.
- The cleanup on cancel should live in one place — a `finally` that awaits
  `rep.orchestrator.wait_until_complete_async()` on the way out.

### Suggested fix

Wrap each workflow's main loop in try/finally:

```python
try:
    for i, frame_idx in enumerate(sampled):
        ...
except asyncio.CancelledError:
    progress_cb(None, "Cancelled", "")
    raise
finally:
    try:
        await rep.orchestrator.wait_until_complete_async()
    except Exception:
        pass
```

Then the upfront `self.recorder.teardown()` calls can be removed as dead
code.

---

## Finding 17 — MEDIUM — `capture_frame` swallows errors, miscounts frames

**File:** `blv/synth/data_collector/backend/capture.py`
**Lines:** 285-301
**Function:** `DataRecorder.capture_frame`

### Bug

```python
async def capture_frame(self) -> None:
    ...
    try:
        await rep.orchestrator.step_async(...)
        self._frame_count += 1
    except Exception as exc:
        carb.log_error(f"[BLV] capture_frame failed at frame {self._frame_count}: {exc}")
```

Two issues:

1. On failure, `_frame_count` is **not** incremented — but the caller
   (`record_with_trajectory`) `captured += 1` unconditionally (`session.py:508`).
   Counter mismatch between what the user is told and what's on disk.
2. `step_async` failures are logged and swallowed. A `step_async` failure
   usually means the render product is in a bad state — continuing to loop
   just produces hundreds of identical error logs and zero usable frames.

### Why this matters

- Silent data gap. Frame indices in the output folder have holes; the
  user doesn't know which frames are missing.
- Root-cause confusion — the user sees "captured 300 frames" in the UI
  but only 287 files on disk.

### Suggested fix

```python
async def capture_frame(self) -> bool:
    if not self._is_setup:
        carb.log_error(...)
        return False
    await rep.orchestrator.step_async(...)   # let errors propagate
    self._frame_count += 1
    return True
```

Caller handles the exception. This is one of those cases where the
defensive `except` does more harm than good — let the workflow decide how
to react.

---

## Finding 18 — MEDIUM — Auto-save fires on every mouse jitter

**File:** `blv/synth/data_collector/backend/session.py`
**Lines:** 54-56 (debounce), 399-416 (`_on_asset_moved`)
**Function:** `_on_asset_moved`

### Bug

```python
_AUTOSAVE_DEBOUNCE_TICKS: int = 0
...
def _on_asset_moved(self, translate, orient, scale):
    ...
    self.locations.save_transform(...)
```

The constant is 0 and the comment admits it. Every tick the
`AssetBrowser._on_tick` detects a transform delta above the eps threshold,
it emits `asset_transform_changed`, which triggers a full JSON rewrite of
`location.json`.

Combined with Finding 14 (non-atomic writes), this maximises the time
window in which the file is vulnerable to corruption on crash. A user
dragging an asset around the viewport for 5 seconds at 60 fps writes
`location.json` ~300 times.

### Why this matters

- Filesystem churn.
- Corruption exposure.
- Noisy inotify / FS watchers on any tooling pointed at the data dir.

### Suggested fix

Debounce with a timer rather than a tick count:

```python
import asyncio
self._autosave_timer: Optional[asyncio.TimerHandle] = None

def _on_asset_moved(self, translate, orient, scale):
    if self._autosave_timer is not None:
        self._autosave_timer.cancel()
    loop = asyncio.get_event_loop()
    self._autosave_timer = loop.call_later(
        0.25, lambda: self._flush_transform(translate, orient, scale)
    )
```

0.25 s is imperceptible to the user, eliminates 99 % of writes during drag.

---

## Finding 19 — MEDIUM — Matrix → Quat factoring loses info on sheared matrices

**File:** `blv/synth/data_collector/backend/asset_browser.py`
**Lines:** 447-459
**Function:** `_read_world_transform`

### Bug

```python
xform = Gf.Transform()
xform.SetMatrix(mat)
orient = Gf.Quatd(xform.GetRotation().GetQuat())
scale  = Gf.Vec3d(xform.GetScale())
```

`Gf.Transform.SetMatrix` runs a polar decomposition. If `mat` contains
shear (from a non-uniform scale composed with a rotation), the decomposition
produces R, S, and a *separate shear component*. `GetRotation()` and
`GetScale()` return R and S, but the shear is discarded silently.

### Why this matters

- Non-uniform scale on a parent Xform + rotation on the prim → shear in
  the world matrix → your "captured spawn transform" loses the shear on
  round-trip. Reloading location.json places the asset slightly off.
- A capture → save → reload cycle should be idempotent; it isn't.

### Suggested fix

Work directly with local ops wherever possible (see Finding 3 — Option A).
If you really need world transform, extract from the parent and the prim
separately, or refuse to capture a transform that has non-zero shear and
surface a user-facing error.

---

## Finding 20 — MEDIUM — Asset stem collision bleeds captures together

**File:** `blv/synth/data_collector/backend/asset_browser.py`
**Lines:** 319-325 (`load_asset` — stem derivation), `backend/session.py:430-439` (`_current_run_name`)
**Functions:** `AssetBrowser.load_asset` + `Session._current_run_name`

### Bug

```python
# asset_browser.py:319
asset_stem = os.path.splitext(os.path.basename(asset_path))[0]
```

Two assets with different parent directories but the same filename (common
when batch-exporting per-category folders, e.g. `chairs/model.usd` and
`tables/model.usd`) produce the same stem. `AssetBrowser` handles this
on the USD side via `get_stage_next_free_path` (line 323) so prim paths
are unique (`/World/model`, `/World/model_01`). But
`Session._current_run_name` uses the stem alone (line 431-433) →
`sanitize_folder_name(stem)` → same output folder.

### Why this matters

- Two assets' captures stomp each other.
- The Replicator writer doesn't know it's overwriting and emits no
  warning.

### Suggested fix

Use the full asset path hash or the parent-dir-plus-stem as the run name:

```python
rel = os.path.relpath(asset_path, self.assets.asset_folder)
return _paths.sanitize_folder_name(os.path.splitext(rel)[0])
```

That gives `chairs_model` vs `tables_model`.

---

## Finding 21 — LOW — dt is unclamped

**File:** `blv/synth/data_collector/backend/gamepad_camera.py`
**Lines:** 389-392
**Function:** `_on_update`

### Bug

```python
try:
    dt: float = event.payload["dt"]
except Exception:
    dt = 1.0 / 60.0
```

Kit can produce a very large `dt` after a stall (the first update after a
stage swap, an alt-tab while loading a scene, etc.). With `look_speed=60.0`
and `dt=2.0`, a frame of stick input produces a 120° instant yaw.

### Why this matters

- Camera teleports after any stall.
- Trajectories recorded during a stall have a single frame with a huge
  delta — playback cannot reproduce it.

### Suggested fix

```python
dt = min(dt, 0.1)   # cap at 100 ms per update
```

---

## Finding 22 — LOW — Warmup frames is a magic constant

**File:** `blv/synth/data_collector/backend/stage.py`
**Lines:** 60-61, 216-217
**Function:** `StageController.switch_to`

### Bug

```python
_WARMUP_FRAMES: int = 20
...
for _ in range(_WARMUP_FRAMES):
    await omni.kit.app.get_app().next_update_async()
```

20 is an empirical number. Big scenes take longer; tiny scenes finish in
2-3 frames and waste time. When the first `step_async` after a swap still
references a half-built scene, the symptom is a black or garbage RGB
frame at frame 0 of the new capture.

### Why this matters

- Works most of the time → looks fine → lingering intermittent bug.
- Wastes ~0.3 s every stage swap on small scenes (20 × 16 ms).

### Suggested fix

Wait on hydra texture settled or on `STAGE_EVENT_ASSETS_LOADED` instead:

```python
from omni.usd import StageEventType
ready = asyncio.Event()
def _on(evt):
    if evt.type == int(StageEventType.ASSETS_LOADED):
        ready.set()
sub = ctx.get_stage_event_stream().create_subscription_to_pop(_on, name="...")
try:
    await asyncio.wait_for(ready.wait(), timeout=30.0)
finally:
    sub.unsubscribe()
```

Fall back to `_WARMUP_FRAMES` if that event isn't available on this Kit
build. Best of both worlds.

---

## Finding 23 — LOW — Prim-name sanitizer doesn't check USD reserved names

**File:** `blv/synth/data_collector/backend/asset_browser.py`
**Lines:** 440-445
**Function:** `_sanitize_prim_name`

### Bug

```python
cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
```

USD prim names additionally must not start with a digit (this is handled
at line 443-444), but also should avoid unicode-trimming artifacts and
should be normalised against existing children before hitting
`get_stage_next_free_path`. The current implementation trusts
`get_stage_next_free_path` for collision resolution, which is fine, but
doesn't document the resulting rename in the log, so a user whose asset
`chair.usd` ends up as `chair_01` has no breadcrumb.

### Why this matters

- Minor UX — the capture goes into a folder named after the stem
  (`chair`) but the prim in the scene is `chair_01`. Hard to correlate
  when debugging.

### Suggested fix

Log when rename happens:

```python
new_path = omni.usd.get_stage_next_free_path(stage, candidate, False)
if new_path != candidate:
    carb.log_info(f"[BLV] Prim path collision — {candidate} → {new_path}")
```

---

## Finding 24 — LOW — `colorize_semantic_segmentation` is a modifier, not an annotator

**File:** `blv/synth/data_collector/backend/capture.py`
**Lines:** 45-55 (defaults), 241-255 (`_build_writer`)
**Function:** `_build_writer`

### Bug

```python
rgb=ann.get("rgb", True),
semantic_segmentation=ann.get("semantic_segmentation", True),
colorize_semantic_segmentation=ann.get("colorize_semantic_segmentation", True),
```

Replicator's `BasicWriter.initialize` treats `colorize_semantic_
segmentation` as a flag that only matters when `semantic_segmentation=True`.
The UI exposes both as independent toggles (`ui/sections/capture.py`
section — not re-read this pass, but it's wired through
`DEFAULT_ANNOTATORS`). A user toggling colorize on while seg is off just
gets no colorized output and no warning.

### Why this matters

- UI lies to the user about what's possible.
- Likewise for `bounding_box_2d_loose` vs `bounding_box_2d_tight` — they're
  independent but share most of the compute.

### Suggested fix

Either:

1. Collapse the UI into logical groups: "Segmentation (Mask | Mask+
   Colorized)" as a radio, not two checkboxes.
2. Or, in `_build_writer`, coerce dependent flags:
   `coloured = ann.get("colorize_semantic_segmentation", True) and
   ann.get("semantic_segmentation", True)`.

---

## Finding 25 — LOW — Task juggling in the UI section

**File:** `blv/synth/data_collector/ui/sections/record_with_trajectory.py`
**Lines:** 79-120
**Functions:** `_on_record`, `_on_record_all`

### Bug

```python
self._task = asyncio.ensure_future(run())
```

Both `_on_record` (line 102) and `_on_record_all` (line 120) assign to
`self._task` without checking whether the previous task is still in
flight. If the user clicks "Record Trajectory" and then "Record All"
before the first finishes, the first task is orphaned (still running,
still writing files, but no longer cancellable through the UI).

### Why this matters

- Clicking Cancel after the double-start cancels only the *second*
  task — the first keeps running and writes files while the user thinks
  they're stopped.

### Suggested fix

```python
if self._task is not None and not self._task.done():
    self.widgets["rwt_status"].text = "Already running — cancel first"
    return
```

Apply to both buttons, and ideally disable the other button while one is
running.

---

## Finding 26 — LOW — CLI signal handling

**File:** `blv/synth/data_collector/cli/bootstrap.py` (from earlier survey)
**Function:** around `signal.signal(signal.SIGINT, ...)`

### Bug (reconstructed)

Calling `signal.signal(SIGINT, ...)` from Python after Kit boots risks
overriding Kit's own SIGINT handler. On Python 3.11, `asyncio` additionally
installs its own handler via `add_signal_handler` — the raw `signal.signal`
path bypasses asyncio's integration, so a Ctrl-C during `await recorder.
capture_frame()` may not cancel the orchestrator cleanly.

### Suggested fix

Install the handler through the loop:

```python
loop = asyncio.get_event_loop()
loop.add_signal_handler(signal.SIGINT, lambda: task.cancel())
```

Only applies when running under the asyncio loop Kit provides.

---

# Architectural observations

Not bugs per se — observations a senior reviewer would write up.

### Session is a god object

`Session` owns every backend module *and* the workflow methods *and* the
gamepad record-toggle callback *and* the auto-save subscription. Most of
the methods are < 20 lines, which is fine, but the 3 capture workflows
(`record_with_trajectory`, `record_all_trajectories`, `collect_all`) each
reimplement the same loop structure:

```
ensure setup → iterate assets → iterate trajectories → per-frame (set_pose →
await next_update → await capture_frame) → progress_cb
```

Extracting a `CaptureLoop` primitive (given a list of `(asset, trajectory,
output_dir)` tuples, do the standard loop) would cut ~200 LOC and centralise
the cancel-on-error handling in Finding 16.

### EventBus has exactly one real consumer

`AssetBrowser.TRANSFORM_CHANGED_EVENT` is the only event with a subscriber
in the shipped code (`Session._on_asset_moved`). The bus adds indirection
without payoff — a direct callback (`assets.on_transform_changed =
session._on_asset_moved`) would be just as decoupled and one layer of
flakiness shallower. `StageController` also emits bus events but no one
consumes `stage_will_close` / `stage_opened` / `stage_closed` /
`hook_error` — pure overhead.

### Per-frame USD reads in `AssetBrowser._on_tick`

`_on_tick` runs `read_current_prim_transform` **every Kit tick**, which is
a USD `GetAttribute` × 3 call sequence. For a 60 Hz tick rate that's 180
USD reads per second just to detect whether the user has moved the prim.
A `Tf.Notice` on `xformOp:*` attribute changes would be event-driven and
avoid the poll. Minor — but it's the sort of thing that shows up in a
profiler when you have 50 assets loaded.

### Tests

`tests/unit/conftest.py` exists but the test files only cover
`trajectory_io`, `location`, `paths`, and `config` (pure-Python modules).
The *hard* parts — `AssetBrowser.load_asset`, `Session.record_with_
trajectory`, `StageController.switch_to`, the gamepad camera — have zero
test coverage because they need a stage. Kit ships a pytest harness
(`omni.kit.test`) that gives you a scratch stage per test — consider
adopting it for the next iteration.

### Config precedence

The comment at the top of `config.py` claims
YAML > carb > hardcoded. That's consistent with the implementation *for
missing/unset values*, but because of Findings 9 and 10 the effective
precedence for zero values is:

```
YAML-nonzero > carb-nonzero > hardcoded > YAML-zero > carb-zero
```

Not what anyone expects.

---

# Priority for remediation

If the time budget is tight, the order I'd fix in is:

1. **Finding 1 (focal length units)** — silent data correctness bug that
   invalidates captures on meters-unit stages. 10-minute fix.
2. **Finding 2 (external stage open)** — crash / leak when users click
   File → Open. 1-2 hour fix depending on route chosen.
3. **Finding 5 (overwrite on re-run)** — silent data loss. 30-minute fix.
4. **Finding 3 (world-vs-local transform)** — silent spawn misalignment
   that propagates through location.json. 2-3 hour fix plus a unit test.
5. **Finding 4 (pitch clamp)** — single-line fix. Do it now.
6. **Finding 7 + 8 (tick-rate trajectories)** — the whole "record once,
   replay many" value proposition depends on this. Half-day fix.
7. Everything else by severity / cheapness.

---

## Appendix: verification notes

Things I explicitly checked by reading source rather than trusting docs:

- `isaacsim.core.utils.xforms.reset_and_set_xform_ops` — confirmed the
  op order is `[translate, orient, scale]` with `PrecisionDouble` (see
  `isaacsim/core/utils/xforms.py` in the installed distro).
- `isaacsim.core.utils.semantics.add_labels` / `remove_labels` —
  confirmed signatures `(prim, labels, instance_name="class", overwrite=
  True)` and `(prim, instance_name="class")`.
- `pxr.Gf.Quatd` constructor — `Quatd(real, i, j, k)` with the 4-float
  form, verified against the code at `backend/session.py:690`:
  `Gf.Quatd(r[0], r[1], r[2], r[3])` matches the pattern (real first).

Things I did *not* verify at the bundled-source level, falling back on
official docs / knowledge:

- USD `focalLength` units — cited from openusd.org/release/api/class_usd_
  geom_camera.html description text.
- Replicator orchestrator `step_async` signature — relied on Omniverse
  Replicator docs.
- Kit `carb.settings.is_setting` API shape — suggested fix uses pseudocode
  because I didn't round-trip the exact method name.
