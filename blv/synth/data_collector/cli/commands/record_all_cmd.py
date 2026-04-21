"""``blv-collect record-all`` — mimic the UI "Record All Trajectories" button.

Headless batch over every asset × every trajectory at a single
environment + single location + single class.  Every required value
comes from the YAML config — CLI flags only override them.

Equivalent GUI flow:
  1. Project Settings → Apply (env + class + resolution + rt)
  2. Asset Browser → pick Location
  3. Capture → Record All Trajectories
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time


def _info(msg: str) -> None:
    """Single-line status print — stdout so Kit doesn't tag each line
    as ``[Error]`` in its log (anything written to stderr under an
    ``omni.kit.app`` process is logged at error level)."""
    print(f"[record-all] {msg}", flush=True)


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "record-all",
        help="headless: mimic UI 'Record All Trajectories' for one env/class/location",
    )
    p.add_argument("--config", default=None, help="Path to YAML config")
    p.add_argument("--env", dest="env",
                   help="Override environment name (folder under environments_folder)")
    p.add_argument("--class", dest="class_name",
                   help="Override asset_class_name from YAML")
    p.add_argument("--location", help="Override location name from YAML")
    p.add_argument("--frame-step", type=int, default=None,
                   help="Capture every Nth frame (default: from YAML, or 1)")
    p.set_defaults(func=run)
    return p


def run(args) -> int:
    # Only pure-Python imports up here.  ``Session`` transitively imports
    # ``carb`` / ``omni.usd`` / ``pxr``, which are only available after
    # ``SimulationApp`` has booted — see the deferred import below.
    from ..bootstrap import start_sim, shutdown
    from ...backend.config import load_config
    from ...backend import paths as _paths

    _info(f"Loading config: {args.config or '(default)'}")
    defaults = load_config(yaml_path=args.config, include_carb=False)

    env = args.env or defaults.environment
    cls = args.class_name or defaults.asset_class_name
    location = args.location or defaults.location
    frame_step = int(args.frame_step or defaults.frame_step or 1)

    envs_root = _paths.normalize(defaults.environments_folder)
    root = _paths.normalize(defaults.root_folder)
    asset_root = _paths.normalize(defaults.asset_root_folder)

    missing = [
        name for name, value in (
            ("environment", env),
            ("asset_class_name", cls),
            ("location", location),
            ("root_folder", root),
            ("environments_folder", envs_root),
            ("asset_root_folder", asset_root),
        ) if not value
    ]
    if missing:
        print(
            "ERROR: missing required config value(s): "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    usd_path = os.path.join(envs_root, env, f"{env}.usd")
    if not os.path.isfile(usd_path):
        print(f"ERROR: USD not found: {usd_path}", file=sys.stderr)
        return 2

    _info(f"env={env}  class={cls}  location={location}  frame_step={frame_step}")
    _info(f"root={root}")
    _info(f"asset_root={asset_root}")
    _info(f"envs_root={envs_root}")
    _info(f"USD={usd_path}")

    t0 = time.monotonic()
    _info("Booting SimulationApp (headless)...")
    app = start_sim(headless=True)
    _info(f"SimulationApp ready in {time.monotonic() - t0:.1f}s")

    try:
        # Deferred: carb/omni are only importable after SimulationApp boots.
        from ...backend.session import Session

        _info("Creating Session + applying project settings")
        session = Session(defaults=defaults)
        session.apply_project_settings(
            root_folder=root,
            environment=env,
            class_name=cls,
            resolution=defaults.resolution,
            rt_subframes=int(defaults.rt_subframes),
        )

        scan_dir = os.path.join(asset_root, cls)
        _info(f"Scanning assets: {scan_dir}")
        n_assets = session.assets.set_folder(scan_dir, class_name=cls)
        if n_assets < 1:
            print(
                f"ERROR: no assets found under {scan_dir}",
                file=sys.stderr,
            )
            session.destroy()
            return 2
        _info(f"Found {n_assets} asset(s)")

        _info(f"Setting location: {location}")
        session.set_location(location)
        traj_names = session.traj_manager.list_trajectory_names()
        if not traj_names:
            print(
                f"ERROR: no trajectories at {session.traj_manager.directory}",
                file=sys.stderr,
            )
            session.destroy()
            return 2
        _info(
            f"Found {len(traj_names)} trajectory file(s) in "
            f"{session.traj_manager.directory}"
        )
        for t in traj_names:
            _info(f"  - {t}")

        # Load the location's spawn transform so every asset starts
        # from the recorded pose.
        try:
            from pxr import Gf  # deferred: pxr needs SimulationApp boot
            loc_data = session.locations.load_location(location)
            t = loc_data["spawn_transform"]["translate"]
            r = loc_data["spawn_transform"]["orient"]
            s = loc_data["spawn_transform"]["scale"]
            session.assets.set_spawn_transform(
                Gf.Vec3d(*t),
                Gf.Quatd(r[0], r[1], r[2], r[3]),
                Gf.Vec3d(*s),
            )
            _info(f"Loaded spawn transform: translate={t}")
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARNING: could not load location transform: {exc}",
                file=sys.stderr,
            )

        async def workflow() -> int:
            _info(f"Loading stage: {usd_path}")
            t_swap = time.monotonic()
            ok = await session.stage.switch_to(usd_path)
            if not ok:
                _info("Stage load FAILED")
                return 1
            _info(f"Stage loaded in {time.monotonic() - t_swap:.1f}s")

            _info(
                f"Starting capture: {n_assets} assets × "
                f"{len(traj_names)} trajectories"
            )
            t_cap = time.monotonic()
            captured = await session.record_all_trajectories(
                frame_step=frame_step,
                progress_cb=_print_progress,
            )
            dt = time.monotonic() - t_cap
            _info(f"Capture finished in {dt:.1f}s — {captured} frames")
            return 0

        task = asyncio.ensure_future(workflow())

        def _cancel_on_sigint(*_):
            if not task.done():
                _info("SIGINT — cancelling task")
                task.cancel()
        signal.signal(signal.SIGINT, _cancel_on_sigint)

        # Hand-pump Kit's frame loop until the workflow task finishes.
        # ``next_update_async`` inside the backend only resolves when
        # ``app.update()`` ticks, so calling ``loop.run_until_complete``
        # here deadlocks.
        try:
            while not task.done():
                app.update()
            rc = task.result()
        except asyncio.CancelledError:
            print("\nCancelled.", file=sys.stderr)
            rc = 130

        _info("Tearing down Session")
        session.destroy()
        return rc
    finally:
        _info("Shutting down SimulationApp")
        shutdown(app)


def _print_progress(fraction, status, detail=""):
    pct = f"{fraction * 100:5.1f}%" if fraction is not None else "  …  "
    line = f"[{pct}] {status}"
    if detail:
        line = f"{line} — {detail}"
    print(line, flush=True)
