"""Config loading for the BLV Synth Data Collector.

Every default the extension ships with can be overridden in three
places, in priority order:

1. ``config/config.yaml`` next to the extension, when present.
2. Carb settings under ``/exts/blv.synth.data_collector/*`` (these come
   from ``config/extension.toml`` or the user's ``omniverse.toml``).
3. Hardcoded fallbacks baked into :class:`Defaults` below.

This module is importable without ``carb`` — when ``carb`` is missing
(unit tests, CLI on vanilla Python) the second tier is skipped and YAML
values fall straight through to the fallbacks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover — yaml ships with Isaac Sim
    yaml = None  # type: ignore

try:
    import carb  # type: ignore
except ImportError:  # pragma: no cover — triggered in unit tests
    carb = None  # type: ignore

_log = logging.getLogger(__name__)


# The default annotator set is duplicated here rather than imported from
# ``capture`` so that ``config`` stays free of any omni/replicator imports.
# ``capture.DEFAULT_ANNOTATORS`` remains the source of truth for what the
# writer actually enables; ``Defaults`` simply mirrors the keys that users
# can flip from YAML.
_DEFAULT_ANNOTATORS: Dict[str, bool] = {
    "rgb": True,
    "semantic_segmentation": True,
    "colorize_semantic_segmentation": True,
    "bounding_box_2d_tight": True,
    "bounding_box_2d_loose": False,
    "bounding_box_3d": False,
    "instance_segmentation": False,
    "normals": False,
    "distance_to_image_plane": False,
}


@dataclass
class Defaults:
    """Resolved default values for every user-configurable setting.

    The field names mirror keys in ``config.yaml`` so
    :func:`load_config` can use :func:`dataclasses.fields` to drive the
    merge without an explicit field-by-field mapping.
    """

    # --- Project paths ---
    root_folder: str = "~/blv_data"
    environment: str = ""
    asset_class_name: str = ""
    asset_root_folder: str = ""
    environments_folder: str = ""

    # --- Camera ---
    camera_path: str = "/World/BLV_Camera"
    move_speed: float = 60.0
    look_speed: float = 30.0
    focal_length: float = 28.0

    # --- Rendering ---
    resolution_width: int = 1280
    resolution_height: int = 720
    rt_subframes: int = 4

    # --- Asset browser ---
    parent_prim_path: str = "/World"

    # --- Annotators (name → enabled flag) ---
    annotators: Dict[str, bool] = field(
        default_factory=lambda: dict(_DEFAULT_ANNOTATORS)
    )

    @property
    def resolution(self) -> tuple:
        return (int(self.resolution_width), int(self.resolution_height))


# --------------------------------------------------------------------- #
#  Loaders                                                               #
# --------------------------------------------------------------------- #


def _default_yaml_path() -> str:
    """Locate ``config/config.yaml`` relative to this file.

    Layout on disk::

        <ext_root>/config/config.yaml
        <ext_root>/blv/synth/data_collector/backend/config.py  ← __file__

    So the extension root is four parents up from this file.
    """
    here = os.path.abspath(__file__)
    ext_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(here))
    )))
    return os.path.join(ext_root, "config", "config.yaml")


def _read_yaml(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path) or yaml is None:
        return {}
    try:
        with open(path, "r") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        if carb is not None:
            carb.log_warn(f"[BLV] Failed to parse config.yaml: {exc}")
        else:
            _log.warning("Failed to parse config.yaml: %s", exc)
        return {}


def _carb_str(ext_key: str, setting_key: str) -> Optional[str]:
    if carb is None:
        return None
    try:
        settings = carb.settings.get_settings()
        val = settings.get_as_string(f"/{ext_key}/{setting_key}")
    except Exception:  # noqa: BLE001
        return None
    return val or None


def _carb_int(ext_key: str, setting_key: str) -> Optional[int]:
    if carb is None:
        return None
    try:
        settings = carb.settings.get_settings()
        val = settings.get_as_int(f"/{ext_key}/{setting_key}")
    except Exception:  # noqa: BLE001
        return None
    return val or None


def _carb_float(ext_key: str, setting_key: str) -> Optional[float]:
    if carb is None:
        return None
    try:
        settings = carb.settings.get_settings()
        val = settings.get_as_float(f"/{ext_key}/{setting_key}")
    except Exception:  # noqa: BLE001
        return None
    return val or None


# Mapping: Defaults field name → (carb setting key, type)
# Only fields that have a corresponding carb setting in extension.toml
# are listed here.  All other fields fall through YAML → hardcoded.
_CARB_BINDINGS: Dict[str, tuple] = {
    "camera_path": ("default_camera_path", "str"),
    "move_speed": ("default_move_speed", "float"),
    "look_speed": ("default_look_speed", "float"),
    "focal_length": ("default_focal_length", "float"),
    "resolution_width": ("default_resolution_width", "int"),
    "resolution_height": ("default_resolution_height", "int"),
    "rt_subframes": ("default_rt_subframes", "int"),
    "root_folder": ("default_root_folder", "str"),
    "environment": ("default_environment", "str"),
    "asset_class_name": ("default_asset_class_name", "str"),
}

_EXT_KEY = "exts.blv.synth.data_collector"


def load_config(
    yaml_path: Optional[str] = None,
    include_carb: bool = True,
) -> Defaults:
    """Build a :class:`Defaults` with values merged across all tiers.

    Parameters
    ----------
    yaml_path:
        Path to a YAML config.  ``None`` → look for the extension's
        default ``config/config.yaml``.  Pass an explicit empty string
        to skip the YAML tier entirely.
    include_carb:
        Set to ``False`` to skip the carb-settings tier (useful for
        unit tests even when carb happens to be importable).
    """
    if yaml_path is None:
        yaml_path = _default_yaml_path()

    cfg = _read_yaml(yaml_path) if yaml_path else {}
    out = Defaults()

    # Legacy alias: older configs used ``asset_folder``.
    if "asset_root_folder" not in cfg and "asset_folder" in cfg:
        cfg["asset_root_folder"] = cfg["asset_folder"]

    for f in fields(Defaults):
        name = f.name

        # Annotators get a special merge: start from defaults, override
        # with any keys supplied in YAML.
        if name == "annotators":
            merged = dict(_DEFAULT_ANNOTATORS)
            yaml_annots = cfg.get("annotators")
            if isinstance(yaml_annots, dict):
                merged.update(
                    {k: bool(v) for k, v in yaml_annots.items() if k in merged}
                )
            setattr(out, name, merged)
            continue

        # 1) YAML — respect explicit keys, even when they hold empty strings
        if name in cfg and cfg[name] is not None:
            try:
                setattr(out, name, _coerce(f.type, cfg[name]))
            except (TypeError, ValueError):
                pass
            continue

        # 2) carb setting, if one is bound
        if include_carb and name in _CARB_BINDINGS:
            setting_key, kind = _CARB_BINDINGS[name]
            if kind == "str":
                v = _carb_str(_EXT_KEY, setting_key)
            elif kind == "int":
                v = _carb_int(_EXT_KEY, setting_key)
            else:
                v = _carb_float(_EXT_KEY, setting_key)
            if v is not None:
                setattr(out, name, v)
                continue

        # 3) fall through to the hardcoded default baked into ``Defaults``.

    return out


def _coerce(target_type: Any, value: Any) -> Any:
    """Best-effort coerce YAML-parsed values to the dataclass field type.

    Typed as ``Any`` because :func:`dataclasses.fields` returns ``str``
    annotations rather than real type objects at runtime.
    """
    if target_type in ("str", str):
        return str(value)
    if target_type in ("int", int):
        return int(value)
    if target_type in ("float", float):
        return float(value)
    return value
