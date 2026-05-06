"""``blv-collect list`` — inventory what's on disk.

No Isaac boot required.  Prints a tree of environments / locations /
trajectories per configured class, plus the asset USDs that will be
referenced in.

Single-class (legacy): ``--class foo`` or YAML ``asset_class_name: foo``.
Multi-class:           ``--classes a,b,c`` or YAML ``classes: [a, b, c]``.
"""

from __future__ import annotations

import argparse
import glob as _glob
import os
import sys
from typing import List

from ...backend import paths as _paths
from ...backend import trajectory_io as _tio
from ...backend.config import load_config


# Mirrors AssetBrowser.USD_EXTENSIONS so the inventory matches what the
# headless capture run would actually pick up.  Pure-glob, no omni.usd
# needed — safe to call without booting Isaac.
_USD_EXTENSIONS: tuple = ("*.usd", "*.usda", "*.usdc", "*.usdz")


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "list", help="list envs/locations/trajectories on disk (no sim boot)",
    )
    p.add_argument("--config", help="Path to config.yaml", default=None)
    p.add_argument("--root", help="Override root_folder", default=None)
    p.add_argument("--envs", help="Override environments_folder", default=None)
    p.add_argument("--asset-root", help="Override asset_root_folder", default=None)
    p.add_argument(
        "--class", dest="class_name",
        help="Override single-class (asset_class_name)", default=None,
    )
    p.add_argument(
        "--classes",
        help=(
            "Comma-separated multi-class list.  Overrides YAML ``classes`` "
            "and ``asset_class_name``."
        ),
        default=None,
    )
    p.add_argument(
        "--no-assets", action="store_true",
        help="Skip the asset USD listing per class (faster for huge folders).",
    )
    p.set_defaults(func=run)
    return p


def _resolve_classes(args, defaults) -> List[str]:
    """``--classes`` > ``--class`` > YAML ``classes`` > YAML ``asset_class_name``."""
    if args.classes:
        return [c.strip() for c in args.classes.split(",") if c.strip()]
    if args.class_name:
        return [args.class_name]
    if defaults.classes:
        return list(defaults.classes)
    if defaults.asset_class_name:
        return [defaults.asset_class_name]
    return []


def _list_assets(asset_root: str, cls: str) -> List[str]:
    if not asset_root:
        return []
    folder = os.path.join(asset_root, cls)
    if not os.path.isdir(folder):
        return []
    return sorted(
        path
        for ext in _USD_EXTENSIONS
        for path in _glob.glob(os.path.join(folder, ext))
    )


def _print_class(
    cls: str,
    root: str,
    envs: str,
    asset_root: str,
    show_assets: bool,
) -> tuple:
    """Print one class's plan.  Returns (n_envs, n_locs, n_trajs, n_assets)."""
    print(f"=== {cls} ===")
    n_assets = 0
    if show_assets:
        assets = _list_assets(asset_root, cls)
        n_assets = len(assets)
        if asset_root:
            asset_dir = os.path.join(asset_root, cls)
            print(f"  assets ({n_assets}) under {asset_dir}:")
            for a in assets:
                print(f"    - {os.path.basename(a)}")
            if n_assets == 0:
                print(f"    (no USD files matching {_USD_EXTENSIONS})")
        else:
            print("  assets: (asset_root_folder unset — skipping asset scan)")

    if envs:
        plans = _paths.plan_collect_all(root, cls, envs)
        if not plans:
            print(
                "  (no environments with location.json + trajectories under "
                f"{os.path.join(root, cls)})"
            )
            print()
            return (0, 0, 0, n_assets)
        n_envs, n_locs, n_trajs = _paths.plan_totals(plans)
        print(f"  matrix: {n_envs} envs | {n_locs} locations | {n_trajs} trajectories")
        for p in plans:
            print(f"    {p.env_name}  ({p.usd_path})")
            for loc_name, trajs in p.locations.items():
                print(f"      {loc_name}  ({len(trajs)} trajectories)")
                for t in trajs:
                    print(f"        - {t}")
        print()
        return (n_envs, n_locs, n_trajs, n_assets)

    # Fallback: walk the data tree directly when envs_folder isn't set.
    n_envs = n_locs = n_trajs = 0
    cls_dir = os.path.join(root, cls)
    if not os.path.isdir(cls_dir):
        print(f"  (no data for class '{cls}' under {root})")
        print()
        return (0, 0, 0, n_assets)
    for env in sorted(os.listdir(cls_dir)):
        env_dir = os.path.join(cls_dir, env)
        if not os.path.isdir(env_dir):
            continue
        n_envs += 1
        print(f"    {env}")
        for loc in sorted(os.listdir(env_dir)):
            loc_dir = os.path.join(env_dir, loc)
            if not os.path.isfile(os.path.join(loc_dir, "location.json")):
                continue
            n_locs += 1
            trajs = _tio.list_trajectory_names(
                os.path.join(loc_dir, "trajectories")
            )
            n_trajs += len(trajs)
            print(f"      {loc}  ({len(trajs)} trajectories)")
            for t in trajs:
                print(f"        - {t}")
    print()
    return (n_envs, n_locs, n_trajs, n_assets)


def run(args) -> int:
    defaults = load_config(yaml_path=args.config, include_carb=False)
    root = _paths.normalize(args.root or defaults.root_folder)
    envs = _paths.normalize(args.envs or defaults.environments_folder) \
        if (args.envs or defaults.environments_folder) else ""
    asset_root = _paths.normalize(
        args.asset_root or defaults.asset_root_folder
    ) if (args.asset_root or defaults.asset_root_folder) else ""

    classes = _resolve_classes(args, defaults)
    if not classes:
        print(
            "ERROR: no classes configured (pass --classes, --class, or set "
            "asset_class_name / classes in YAML).",
            file=sys.stderr,
        )
        return 2

    print(f"root_folder:    {root}")
    print(f"envs_folder:    {envs or '(unset)'}")
    print(f"asset_root:     {asset_root or '(unset)'}")
    print(f"classes ({len(classes)}): {', '.join(classes)}")
    print()

    grand_envs = grand_locs = grand_trajs = grand_assets = 0
    empty_classes: List[str] = []
    for cls in classes:
        n_envs, n_locs, n_trajs, n_assets = _print_class(
            cls, root, envs, asset_root, show_assets=not args.no_assets,
        )
        grand_envs += n_envs
        grand_locs += n_locs
        grand_trajs += n_trajs
        grand_assets += n_assets
        if n_trajs == 0 or (asset_root and n_assets == 0):
            empty_classes.append(cls)

    print("=" * 60)
    print(
        f"TOTALS across {len(classes)} class(es): "
        f"{grand_envs} envs | {grand_locs} locations | "
        f"{grand_trajs} trajectories | {grand_assets} assets"
    )
    if empty_classes:
        print(
            f"WARNING: {len(empty_classes)} class(es) have nothing to "
            f"collect: {', '.join(empty_classes)}",
            file=sys.stderr,
        )
    return 0
