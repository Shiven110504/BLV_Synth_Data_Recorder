"""UI package — thin ``omni.ui`` shell that delegates every workflow to
:class:`blv.synth.data_collector.backend.session.Session`.

The old monolithic ``ui.py`` at the project root is kept as a legacy
fallback for one release before it is deleted.
"""

from __future__ import annotations

try:
    from .window import DataCollectorWindow  # noqa: F401
except ImportError:
    # Allow import without omni.ui (unit tests / CLI).
    DataCollectorWindow = None  # type: ignore[assignment]
