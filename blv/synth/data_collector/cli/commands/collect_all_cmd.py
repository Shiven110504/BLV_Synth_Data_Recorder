"""``blv-collect collect-all`` — full matrix headless batch.

Iterates every environment × location × asset × trajectory according
to ``paths.plan_collect_all`` and captures each with the shared
:class:`Session.collect_all` implementation — the same one the UI uses.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "collect-all",
        help="headless batch over envs × locations × assets × trajectories",
    )
    p.add_argument("--config", default=None, help="Path to config.yaml")
    p.add_argument("--frame-step", type=int, default=None,
                   help="Capture every Nth frame (default: from YAML or 1)")
    p.add_argument("--on-error", choices=("skip", "abort"), default="skip")
    p.set_defaults(func=run)
    return p


def run(args) -> int:
    from ..bootstrap import start_sim, shutdown
    from ...backend.config import load_config
    from ...backend.session import Session
    from ...backend import paths as _paths

    defaults = load_config(yaml_path=args.config, include_carb=False)
    envs_folder = _paths.normalize(defaults.environments_folder)
    if not envs_folder:
        print("ERROR: environments_folder is required in config.yaml", file=sys.stderr)
        return 2
    if not defaults.asset_class_name:
        print("ERROR: asset_class_name is required in config.yaml", file=sys.stderr)
        return 2

    app = start_sim(headless=True)
    try:
        session = Session(defaults=defaults)
        session.apply_project_settings(
            root_folder=_paths.normalize(defaults.root_folder),
            environment=defaults.environment,
            class_name=defaults.asset_class_name,
        )
        # Point the asset browser at the correct subfolder.
        try:
            import os
            asset_root = _paths.normalize(defaults.asset_root_folder)
            scan_dir = os.path.join(asset_root, defaults.asset_class_name)
            session.assets.set_folder(
                scan_dir, class_name=defaults.asset_class_name,
            )
        except Exception as exc:
            print(f"WARNING: could not set asset folder: {exc}", file=sys.stderr)

        step = args.frame_step or 1

        # SIGINT cancels the running task and lets ``finally`` do a clean close.
        loop = asyncio.get_event_loop()

        async def workflow():
            return await session.collect_all(
                envs_folder=envs_folder,
                frame_step=step,
                on_env_error=args.on_error,
                progress_cb=_print_progress,
            )

        task = loop.create_task(workflow())

        def _cancel_on_sigint(*_):
            if not task.done():
                task.cancel()
        signal.signal(signal.SIGINT, _cancel_on_sigint)

        try:
            captured = loop.run_until_complete(task)
        except asyncio.CancelledError:
            print("\nCancelled.", file=sys.stderr)
            captured = 0

        print(f"\nTotal captured frames: {captured}")
        session.destroy()
        return 0
    finally:
        shutdown(app)


def _print_progress(fraction, status, detail=""):
    pct = f"{fraction * 100:5.1f}%" if fraction is not None else "  …  "
    line = f"[{pct}] {status}"
    if detail:
        line = f"{line} — {detail}"
    print(line)
