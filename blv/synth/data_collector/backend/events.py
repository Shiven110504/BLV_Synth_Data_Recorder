"""Tiny in-process event bus for decoupling backend modules.

The bus exists so that, for example, :class:`AssetBrowser` can emit
``asset_transform_changed`` without having a direct reference to the
:class:`LocationManager` that should persist the change.  The UI /
``Session`` wires subscribers once on construction and the modules stay
ignorant of each other.

This is deliberately the simplest thing that could work — no priorities,
no async dispatch, no weak refs.  Callbacks run synchronously in
``emit``'s call frame, and an exception from one subscriber is logged
but does not prevent the remaining subscribers from running.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

try:
    import carb  # type: ignore

    def _log_warn(msg: str) -> None:
        carb.log_warn(msg)
except ImportError:  # pragma: no cover — triggered in unit tests
    _log = logging.getLogger(__name__)

    def _log_warn(msg: str) -> None:
        _log.warning(msg)


Callback = Callable[..., Any]


class EventBus:
    """A synchronous, string-keyed publish/subscribe helper."""

    def __init__(self) -> None:
        self._subs: Dict[str, List[Callback]] = {}

    def subscribe(self, name: str, fn: Callback) -> None:
        """Register *fn* to receive ``emit(name, ...)`` calls."""
        self._subs.setdefault(name, []).append(fn)

    def unsubscribe(self, name: str, fn: Callback) -> None:
        """Remove a previously-registered subscriber.

        Silent no-op if *fn* was never subscribed to *name*.
        """
        if name not in self._subs:
            return
        try:
            self._subs[name].remove(fn)
        except ValueError:
            pass

    def emit(self, name: str, *args: Any, **kwargs: Any) -> None:
        """Fire all subscribers of *name*.

        Exceptions from individual subscribers are logged and swallowed
        so one misbehaving listener can't poison the bus.
        """
        for fn in list(self._subs.get(name, [])):
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — intentional broad catch
                _log_warn(
                    f"[BLV] EventBus subscriber for '{name}' raised: {exc!r}"
                )

    def clear(self, name: str = "") -> None:
        """Drop all subscribers for *name*, or for the entire bus if empty."""
        if name:
            self._subs.pop(name, None)
        else:
            self._subs.clear()
