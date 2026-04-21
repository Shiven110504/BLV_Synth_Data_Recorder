"""Headless ``SimulationApp`` boot helper.

Called by every CLI subcommand except ``list`` (which is pure-Python
and wants to stay fast).  Returns the SimulationApp so the caller can
``close()`` it once the workflow finishes.

Usage::

    from blv.synth.data_collector.cli import bootstrap
    app = bootstrap.start_sim(headless=True)
    try:
        ...
    finally:
        bootstrap.shutdown(app)
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def start_sim(
    headless: bool = True,
    renderer: str = "RaytracedLighting",
    extra: Optional[Dict[str, Any]] = None,
):
    """Start a SimulationApp with sensible defaults for batch capture.

    ``sync_loads=True`` blocks until every USD reference has loaded —
    that's what we want for deterministic capture.
    """
    # Imported inside the function so ``blv-collect list`` doesn't pay
    # the ~4 s SimulationApp startup tax.
    from isaacsim import SimulationApp  # type: ignore

    cfg: Dict[str, Any] = {
        "headless": bool(headless),
        "renderer": renderer,
        "sync_loads": True,
    }
    if extra:
        cfg.update(extra)

    app = SimulationApp(cfg)

    import omni.kit.app  # noqa: F401 — only importable after SimulationApp init

    ext_mgr = omni.kit.app.get_app().get_extension_manager()
    ext_mgr.set_extension_enabled_immediate("blv.synth.data_collector", True)
    return app


def shutdown(app) -> None:
    """Best-effort SimulationApp teardown.  Safe to call more than once."""
    if app is None:
        return
    try:
        app.close(wait_for_replicator=True)
    except TypeError:
        try:
            app.close()
        except Exception:
            pass
    except Exception:
        pass
