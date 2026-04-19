"""Stub Isaac Sim / Omniverse modules so pure backend code is importable.

The modules listed below are *module scope* imports somewhere under
``blv.synth.data_collector.backend``.  Without stubs, collecting tests
on a vanilla Python install fails with ``ModuleNotFoundError``.

Each stub is a :class:`types.ModuleType` with a ``MagicMock`` ``_placeholder``.
Modules that downstream code introspects (``carb`` for ``log_*``,
``carb.input.GamepadInput`` as an enum) are given the minimum surface
the backend uses.  Pure modules use ``try/except ImportError`` around
their ``carb`` imports, so stubs here only matter for modules that the
tests actively import; anything else is installed defensively.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _install_stub(name: str, **attrs) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# ``carb`` — the pure modules (paths/config/events/trajectory_io/location)
# guard their imports, but some tests assert logging didn't explode, so
# we give them no-op loggers.
_carb = _install_stub(
    "carb",
    log_info=lambda msg: None,
    log_warn=lambda msg: None,
    log_warning=lambda msg: None,
    log_error=lambda msg: None,
)

# carb.settings
_carb_settings = _install_stub("carb.settings")
_carb_settings.get_settings = MagicMock(return_value=MagicMock())
_carb.settings = _carb_settings

# carb.input — GamepadInput ideally would be an IntEnum, but the pure
# tests never touch gamepad code so a mock is fine.
_carb_input = _install_stub(
    "carb.input",
    GamepadInput=MagicMock(),
    IInput=MagicMock,
    acquire_input_interface=MagicMock(return_value=MagicMock()),
)
_carb.input = _carb_input


# omni.* — only the submodules imported at module scope in backend/.
_omni = _install_stub("omni")
_omni_kit = _install_stub("omni.kit")
_omni_kit_app = _install_stub(
    "omni.kit.app",
    get_app=MagicMock(return_value=MagicMock()),
)
_omni_kit.app = _omni_kit_app
_omni_kit_commands = _install_stub(
    "omni.kit.commands",
    execute=MagicMock(),
)
_omni_kit.commands = _omni_kit_commands
_omni_kit_viewport = _install_stub("omni.kit.viewport")
_omni_kit_viewport_utility = _install_stub(
    "omni.kit.viewport.utility",
    get_active_viewport=MagicMock(return_value=None),
)
_omni_kit_viewport.utility = _omni_kit_viewport_utility

_omni_usd = _install_stub(
    "omni.usd",
    get_context=MagicMock(return_value=MagicMock()),
    get_stage_next_free_path=MagicMock(),
    StageEventType=MagicMock(),
)
_omni.usd = _omni_usd

_omni_appwindow = _install_stub(
    "omni.appwindow",
    get_default_app_window=MagicMock(return_value=MagicMock()),
)
_omni.appwindow = _omni_appwindow

_omni.kit = _omni_kit

_omni_replicator = _install_stub("omni.replicator")
_omni_replicator_core = _install_stub(
    "omni.replicator.core",
    orchestrator=MagicMock(),
    writers=MagicMock(),
    create=MagicMock(),
)
_omni_replicator.core = _omni_replicator_core


# pxr — the pure tests don't touch Gf types, but imports at module scope
# still need to succeed.
_pxr = _install_stub(
    "pxr",
    Gf=MagicMock(),
    Sdf=MagicMock(),
    Usd=MagicMock(),
    UsdGeom=MagicMock(),
)


# isaacsim.core.utils.xforms — imported at module scope by AssetBrowser.
_isaacsim = _install_stub("isaacsim")
_isaacsim_core = _install_stub("isaacsim.core")
_isaacsim_core_utils = _install_stub("isaacsim.core.utils")
_isaacsim_core_utils_xforms = _install_stub(
    "isaacsim.core.utils.xforms",
    reset_and_set_xform_ops=MagicMock(),
)
_isaacsim_core_utils.xforms = _isaacsim_core_utils_xforms
_isaacsim_core_utils_semantics = _install_stub(
    "isaacsim.core.utils.semantics",
    add_labels=MagicMock(),
    remove_labels=MagicMock(),
)
_isaacsim_core_utils.semantics = _isaacsim_core_utils_semantics
_isaacsim_core.utils = _isaacsim_core_utils
_isaacsim.core = _isaacsim_core
