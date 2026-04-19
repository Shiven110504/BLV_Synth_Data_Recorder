"""Backend package for the BLV Synth Data Collector extension.

Pure-Python + Isaac-Sim-facing modules that encapsulate all non-UI
behavior.  The UI (``blv.synth.data_collector.ui``) and the headless CLI
(``blv.synth.data_collector.cli``) both import from this package.

The modules are layered:

* ``paths``, ``events``, ``config``, ``trajectory_io``, ``location`` —
  pure Python, no ``omni``/``carb``/``pxr`` imports at module scope.
  Importable from unit tests without Isaac Sim on the PYTHONPATH.
* ``gamepad_camera``, ``trajectory``, ``asset_browser``, ``capture``,
  ``stage`` — Isaac-Sim-facing; rely on omni / pxr / carb at import time.
* ``session`` — top-level orchestrator consumed by UI + CLI.
"""

from __future__ import annotations
