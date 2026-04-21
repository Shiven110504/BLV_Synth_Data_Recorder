"""
BLVSynthDataCollectorExtension — main ``omni.ext.IExt`` entry point.
=====================================================================

This is the extension lifecycle class registered in ``extension.toml`` via the
``[[python.module]]`` entry.  Kit calls :meth:`on_startup` when the extension is
enabled and :meth:`on_shutdown` when it is disabled (or the app exits).

Responsibilities
----------------
* Create and destroy the :class:`DataCollectorWindow` UI.
* Register / remove the *Window → BLV Synth Data Collector* editor menu item.
"""

from __future__ import annotations

from typing import Optional

import carb
import omni.ext
import omni.kit.ui


class BLVSynthDataCollectorExtension(omni.ext.IExt):
    """Omniverse Kit extension for BLV synthetic data collection v2."""

    MENU_PATH: str = "Window/BLV Synth Data Collector"

    def __init__(self) -> None:
        super().__init__()
        self._window = None
        self._menu = None

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def on_startup(self, ext_id: str) -> None:
        """Called by Kit when the extension is enabled.

        Parameters
        ----------
        ext_id : str
            Fully-qualified extension identifier (e.g.
            ``blv.synth.data_collector-2.0.0``).
        """
        carb.log_info(f"[BLV] BLVSynthDataCollectorExtension starting up (ext_id={ext_id})")

        # Lazy import so that the heavy module graph is only loaded when the
        # extension is actually enabled.  ``.ui`` is now a package
        # containing window.py + sections/*.py.
        from .ui.window import DataCollectorWindow

        self._window = DataCollectorWindow()

        # Register a toggle menu item in the editor's Window menu
        try:
            editor_menu = omni.kit.ui.get_editor_menu()
            self._menu = editor_menu.add_item(
                self.MENU_PATH,
                self._on_menu_click,
                toggle=True,
                value=True,
            )
        except Exception as exc:
            # Menu registration is non-critical — the window can still be used
            carb.log_warn(f"[BLV] Could not register menu item: {exc}")
            self._menu = None

        carb.log_info("[BLV] Extension startup complete.")

    def on_shutdown(self) -> None:
        """Called by Kit when the extension is disabled or the app shuts down."""
        carb.log_info("[BLV] BLVSynthDataCollectorExtension shutting down.")

        # Remove menu entry
        if self._menu is not None:
            try:
                omni.kit.ui.get_editor_menu().remove_item(self._menu)
            except Exception as exc:
                carb.log_warn(f"[BLV] Menu removal warning: {exc}")
            self._menu = None

        # Destroy the window (which also tears down all back-end modules)
        if self._window is not None:
            self._window.destroy()
            self._window = None

        carb.log_info("[BLV] Extension shutdown complete.")

    # ------------------------------------------------------------------ #
    #  Menu callback                                                      #
    # ------------------------------------------------------------------ #

    def _on_menu_click(self, menu_item, checked: bool) -> None:
        """Toggle the window visibility when the menu item is clicked."""
        if self._window is not None:
            self._window.visible = checked
