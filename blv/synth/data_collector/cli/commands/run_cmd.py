"""``blv-collect run`` — single env/class/trajectory headless capture.

The simplest smoke-test for the headless boot path.  Boots Isaac,
opens one env USD, cycles every asset under the class folder and
captures the trajectory against each.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "run", help="headless: capture one trajectory in one env for one class",
    )
    p.add_argument("--config", default=None)
    p.add_argument("--env", required=True, help="Environment name (folder under --envs)")
    p.add_argument("--class", dest="class_name", required=True)
    p.add_argument("--trajectory", required=True,
                   help="Trajectory filename (e.g. traj_01.json)")
    p.add_argument("--location", default=None, help="Location directory name")
    p.add_argument("--frame-step", type=int, default=1)
    p.set_defaults(func=run)
    return p


def run(args) -> int:
    from ... import cli as _cli  # noqa: F401
    from ..bootstrap import start_sim, shutdown
    from ...backend.config import load_config
    from ...backend.session import Session
    from ...backend import paths as _paths

    defaults = load_config(yaml_path=args.config, include_carb=False)
    envs_root = _paths.normalize(defaults.environments_folder)
    usd_path = os.path.join(envs_root, args.env, f"{args.env}.usd")
    if not os.path.isfile(usd_path):
        print(f"ERROR: USD not found: {usd_path}", file=sys.stderr)
        return 2

    app = start_sim(headless=True)
    try:
        session = Session(defaults=defaults)
        session.apply_project_settings(
            root_folder=_paths.normalize(defaults.root_folder),
            environment=args.env,
            class_name=args.class_name,
        )
        if args.location:
            session.set_location(args.location)

        async def workflow():
            ok = await session.stage.switch_to(usd_path)
            if not ok:
                return 1
            captured = await session.record_with_trajectory(
                args.trajectory, frame_step=args.frame_step,
                progress_cb=_print_progress,
            )
            print(f"\nCaptured {captured} frames.")
            return 0

        rc = asyncio.get_event_loop().run_until_complete(workflow())
        session.destroy()
        return rc
    finally:
        shutdown(app)


def _print_progress(fraction, status, detail=""):
    pct = f"{fraction * 100:5.1f}%" if fraction is not None else "  …  "
    line = f"  [{pct}] {status}"
    if detail:
        line = f"{line} — {detail}"
    print(line)
