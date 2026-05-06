"""``blv-collect collect-classes`` — multi-class headless data regeneration.

Like ``collect-all`` but loops over a configurable list of asset
classes.  For each class we re-apply project settings on the same
``Session`` (no SimulationApp restart) and run the full env × location
× asset × trajectory matrix.

The class list comes from the YAML ``classes:`` field, with a CLI
override.  Falls back to ``asset_class_name`` so existing single-class
configs keep working.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time


def _info(msg: str) -> None:
    """Stdout status print — Kit logs anything on stderr at error level."""
    print(f"[collect-classes] {msg}", flush=True)


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "collect-classes",
        help=(
            "headless: multi-class regeneration — "
            "every class × env × loc × asset × traj matrix"
        ),
    )
    p.add_argument("--config", default=None, help="Path to YAML config")
    p.add_argument(
        "--classes",
        default=None,
        help=(
            "Comma-separated class list.  Overrides the YAML ``classes`` "
            "field and ``asset_class_name``."
        ),
    )
    p.add_argument(
        "--frame-step", type=int, default=None,
        help="Capture every Nth frame (default: from YAML, or 1)",
    )
    p.add_argument(
        "--on-error", choices=("skip", "abort"), default="skip",
        help="How to handle env load failures (per-env, inside one class)",
    )
    p.add_argument(
        "--on-class-error", choices=("skip", "abort"), default="skip",
        help=(
            "How to handle class-level failures (no plans, no assets, or "
            "an exception during that class's collect_all)"
        ),
    )
    p.add_argument(
        "--load-timeout", type=float, default=300.0,
        help=(
            "Seconds to wait for open_stage_async per env before treating "
            "the env as a load failure.  0 disables the timeout."
        ),
    )
    p.set_defaults(func=run)
    return p


def _resolve_classes(args, defaults) -> list:
    """Resolve the class list from --classes > YAML classes > asset_class_name."""
    if args.classes:
        return [c.strip() for c in args.classes.split(",") if c.strip()]
    if defaults.classes:
        return list(defaults.classes)
    if defaults.asset_class_name:
        return [defaults.asset_class_name]
    return []


def run(args) -> int:
    # Pure-Python imports up here — Session/carb/omni come after Sim boot.
    from ..bootstrap import start_sim, shutdown
    from ...backend.config import load_config
    from ...backend import paths as _paths

    _info(f"Loading config: {args.config or '(default)'}")
    defaults = load_config(yaml_path=args.config, include_carb=False)

    classes = _resolve_classes(args, defaults)
    frame_step = int(args.frame_step or defaults.frame_step or 1)
    envs_folder = _paths.normalize(defaults.environments_folder)
    root = _paths.normalize(defaults.root_folder)
    asset_root = _paths.normalize(defaults.asset_root_folder)
    load_timeout = args.load_timeout if args.load_timeout > 0 else None

    missing = [
        name for name, value in (
            ("classes (or asset_class_name)", classes),
            ("root_folder", root),
            ("environments_folder", envs_folder),
            ("asset_root_folder", asset_root),
        ) if not value
    ]
    if missing:
        print(
            "ERROR: missing required config value(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    _info(
        f"classes={classes}  frame_step={frame_step}  "
        f"on_error={args.on_error}  on_class_error={args.on_class_error}  "
        f"load_timeout={load_timeout}"
    )
    _info(f"root={root}")
    _info(f"asset_root={asset_root}")
    _info(f"envs_folder={envs_folder}")

    # Pre-flight: print every class's matrix BEFORE booting Sim.  This
    # gives faster failure when a class's data tree is empty and shows
    # the user exactly what's about to be regenerated.
    plans_by_class = {}
    for cls in classes:
        plans = _paths.plan_collect_all(root, cls, envs_folder)
        plans_by_class[cls] = plans
        if not plans:
            _info(f"  [{cls}] WARN: no envs with location.json + trajectories")
            continue
        n_envs, n_locs, n_trajs = _paths.plan_totals(plans)
        _info(
            f"  [{cls}] Plan: {n_envs} envs × {n_locs} locations × "
            f"{n_trajs} trajectories"
        )

    classes_with_plans = [c for c in classes if plans_by_class[c]]
    if not classes_with_plans:
        print(
            "ERROR: every requested class produced an empty plan",
            file=sys.stderr,
        )
        return 2
    if len(classes_with_plans) < len(classes) and args.on_class_error == "abort":
        print(
            "ERROR: some classes had empty plans and --on-class-error=abort",
            file=sys.stderr,
        )
        return 2

    t0 = time.monotonic()
    _info("Booting SimulationApp (headless)...")
    app = start_sim(headless=True)
    _info(f"SimulationApp ready in {time.monotonic() - t0:.1f}s")

    overall_total = 0
    failures: list = []
    try:
        # Deferred: carb/omni are only importable after SimulationApp boots.
        from ...backend.session import Session

        _info("Creating Session")
        session = Session(defaults=defaults)

        for cls_idx, cls in enumerate(classes_with_plans):
            _info(
                f"=== Class {cls_idx + 1}/{len(classes_with_plans)}: {cls} ==="
            )

            try:
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
                    msg = f"no assets found under {scan_dir}"
                    _info(f"  [{cls}] WARN: {msg}")
                    failures.append((cls, msg))
                    if args.on_class_error == "abort":
                        raise RuntimeError(f"[{cls}] {msg}")
                    continue
                _info(f"  [{cls}] Found {n_assets} asset(s)")

                async def workflow(c=cls) -> int:
                    t_cap = time.monotonic()
                    result = await session.collect_all(
                        envs_folder=envs_folder,
                        frame_step=frame_step,
                        on_env_error=args.on_error,
                        progress_cb=_print_progress,
                        load_timeout=load_timeout,
                    )
                    _info(
                        f"  [{c}] collect_all finished in "
                        f"{time.monotonic() - t_cap:.1f}s — {result} frames"
                    )
                    return result

                task = asyncio.ensure_future(workflow())

                def _cancel_on_sigint(*_):
                    if not task.done():
                        _info("SIGINT — cancelling task")
                        task.cancel()
                signal.signal(signal.SIGINT, _cancel_on_sigint)

                try:
                    while not task.done():
                        app.update()
                    captured = task.result()
                except asyncio.CancelledError:
                    print("\nCancelled.", file=sys.stderr)
                    raise
                overall_total += captured

            except asyncio.CancelledError:
                # Propagate out of the per-class loop — the outer finally
                # tears down the Session and SimulationApp.
                raise
            except Exception as exc:  # noqa: BLE001
                _info(f"  [{cls}] ERROR: {exc}")
                failures.append((cls, str(exc)))
                if args.on_class_error == "abort":
                    raise

        print(f"\nTotal captured frames: {overall_total}")
        if failures:
            print(f"Classes with issues ({len(failures)}):", file=sys.stderr)
            for c, msg in failures:
                print(f"  - {c}: {msg}", file=sys.stderr)

        _info("Tearing down Session")
        session.destroy()
        return 0 if not failures else 1
    finally:
        _info("Shutting down SimulationApp")
        shutdown(app)


def _print_progress(fraction, status, detail=""):
    pct = f"{fraction * 100:5.1f}%" if fraction is not None else "  …  "
    line = f"[{pct}] {status}"
    if detail:
        line = f"{line} — {detail}"
    print(line, flush=True)
