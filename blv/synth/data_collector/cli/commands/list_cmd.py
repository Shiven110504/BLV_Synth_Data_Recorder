"""``blv-collect list`` — inventory what's on disk.

No Isaac boot required.  Prints a tree of environments / locations /
trajectories for the configured class.
"""

from __future__ import annotations

import argparse
import os
import sys

from ...backend import paths as _paths
from ...backend import trajectory_io as _tio
from ...backend.config import load_config


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "list", help="list envs/locations/trajectories on disk (no sim boot)",
    )
    p.add_argument("--config", help="Path to config.yaml", default=None)
    p.add_argument("--root", help="Override root_folder", default=None)
    p.add_argument("--envs", help="Override environments_folder", default=None)
    p.add_argument("--class", dest="class_name",
                   help="Override asset_class_name", default=None)
    p.set_defaults(func=run)
    return p


def run(args) -> int:
    defaults = load_config(yaml_path=args.config, include_carb=False)
    root = args.root or defaults.root_folder
    envs = args.envs or defaults.environments_folder
    cls = args.class_name or defaults.asset_class_name

    root = _paths.normalize(root)
    envs = _paths.normalize(envs) if envs else ""

    if not cls:
        print("ERROR: no class name configured (pass --class or set asset_class_name).",
              file=sys.stderr)
        return 2

    print(f"root_folder:    {root}")
    print(f"envs_folder:    {envs or '(unset)'}")
    print(f"class:          {cls}")
    print()

    if envs:
        plans = _paths.plan_collect_all(root, cls, envs)
        if not plans:
            print("(no environments with location.json + trajectories found)")
            return 0
        n_envs, n_locs, n_trajs = _paths.plan_totals(plans)
        print(f"{n_envs} envs | {n_locs} locations | {n_trajs} trajectories")
        print()
        for p in plans:
            print(f"  {p.env_name}  ({p.usd_path})")
            for loc_name, trajs in p.locations.items():
                print(f"    {loc_name}")
                for t in trajs:
                    print(f"      - {t}")
    else:
        # No envs folder — fall back to class/env scan under root.
        if not os.path.isdir(os.path.join(root, cls)):
            print(f"(no data for class '{cls}' under {root})")
            return 0
        for env in sorted(os.listdir(os.path.join(root, cls))):
            env_dir = os.path.join(root, cls, env)
            if not os.path.isdir(env_dir):
                continue
            print(f"  {env}")
            for loc in sorted(os.listdir(env_dir)):
                loc_dir = os.path.join(env_dir, loc)
                if not os.path.isfile(os.path.join(loc_dir, "location.json")):
                    continue
                trajs = _tio.list_trajectory_names(
                    os.path.join(loc_dir, "trajectories")
                )
                print(f"    {loc}  ({len(trajs)} trajectories)")
                for t in trajs:
                    print(f"      - {t}")

    return 0
