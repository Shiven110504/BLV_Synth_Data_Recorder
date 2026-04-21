"""``blv-collect collect-all`` — mimic the UI "Collect All Data" button.

Full matrix: every env × every location × every asset × every
trajectory discovered under ``environments_folder``.  Equivalent to
clicking Start in the Collect All Data section of the UI.

Every knob comes from the YAML config — CLI flags only override.
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
    print(f"[collect-all] {msg}", flush=True)


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "collect-all",
        help="headless: mimic UI 'Collect All Data' — full env × loc × asset × traj matrix",
    )
    p.add_argument("--config", default=None, help="Path to YAML config")
    p.add_argument("--class", dest="class_name",
                   help="Override asset_class_name from YAML")
    p.add_argument("--frame-step", type=int, default=None,
                   help="Capture every Nth frame (default: from YAML, or 1)")
    p.add_argument("--on-error", choices=("skip", "abort"), default="skip",
                   help="How to handle env load failures")
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

    cls = args.class_name or defaults.asset_class_name
    frame_step = int(args.frame_step or defaults.frame_step or 1)
    envs_folder = _paths.normalize(defaults.environments_folder)
    root = _paths.normalize(defaults.root_folder)
    asset_root = _paths.normalize(defaults.asset_root_folder)

    missing = [
        name for name, value in (
            ("asset_class_name", cls),
            ("root_folder", root),
            ("environments_folder", envs_folder),
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

    _info(f"class={cls}  frame_step={frame_step}  on_error={args.on_error}")
    _info(f"root={root}")
    _info(f"asset_root={asset_root}")
    _info(f"envs_folder={envs_folder}")

    # Preview the matrix BEFORE booting Isaac — faster failure, and
    # the user sees exactly what's about to be captured.
    plans = _paths.plan_collect_all(root, cls, envs_folder)
    if not plans:
        print(
            f"ERROR: no environments with location.json + trajectories "
            f"under {envs_folder}",
            file=sys.stderr,
        )
        return 2
    n_envs, n_locs, n_trajs = _paths.plan_totals(plans)
    _info(f"Plan: {n_envs} envs × {n_locs} locations × {n_trajs} trajectories")
    for p in plans:
        _info(f"  env: {p.env_name}  ({p.usd_path})")
        for loc_name, trajs in p.locations.items():
            _info(f"    loc: {loc_name}  ({len(trajs)} trajectories)")

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
            environment=defaults.environment,
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

        async def workflow() -> int:
            t_cap = time.monotonic()
            result = await session.collect_all(
                envs_folder=envs_folder,
                frame_step=frame_step,
                on_env_error=args.on_error,
                progress_cb=_print_progress,
            )
            _info(
                f"collect_all finished in {time.monotonic() - t_cap:.1f}s — "
                f"{result} frames"
            )
            return result

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
            captured = task.result()
        except asyncio.CancelledError:
            print("\nCancelled.", file=sys.stderr)
            captured = 0

        print(f"\nTotal captured frames: {captured}")
        _info("Tearing down Session")
        session.destroy()
        return 0
    finally:
        _info("Shutting down SimulationApp")
        shutdown(app)


def _print_progress(fraction, status, detail=""):
    pct = f"{fraction * 100:5.1f}%" if fraction is not None else "  …  "
    line = f"[{pct}] {status}"
    if detail:
        line = f"{line} — {detail}"
    print(line, flush=True)
