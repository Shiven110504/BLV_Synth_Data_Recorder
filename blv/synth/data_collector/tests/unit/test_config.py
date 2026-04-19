"""Tests for :mod:`blv.synth.data_collector.backend.config`."""

from __future__ import annotations

import textwrap

import pytest

from blv.synth.data_collector.backend import config as cfg


def _write_yaml(tmp_path, contents: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(contents))
    return str(p)


def test_defaults_are_stable_when_no_yaml():
    d = cfg.load_config(yaml_path="", include_carb=False)
    assert d.camera_path == "/World/BLV_Camera"
    assert d.resolution_width == 1280
    assert d.resolution_height == 720
    assert d.move_speed == 60.0
    assert d.look_speed == 30.0
    assert d.root_folder == "~/blv_data"


def test_yaml_overrides_hardcoded(tmp_path):
    path = _write_yaml(tmp_path, """
        root_folder: "/tmp/custom_root"
        move_speed: 12.5
        resolution_width: 1920
        resolution_height: 1080
    """)
    d = cfg.load_config(yaml_path=path, include_carb=False)
    assert d.root_folder == "/tmp/custom_root"
    assert d.move_speed == 12.5
    assert d.resolution_width == 1920
    assert d.resolution_height == 1080
    # Un-set fields fall through to defaults
    assert d.look_speed == 30.0


def test_explicit_empty_string_is_preserved(tmp_path):
    path = _write_yaml(tmp_path, """
        environment: ""
    """)
    d = cfg.load_config(yaml_path=path, include_carb=False)
    assert d.environment == ""


def test_annotators_merge_keeps_defaults_for_unmentioned(tmp_path):
    path = _write_yaml(tmp_path, """
        annotators:
          rgb: false
          instance_segmentation: true
    """)
    d = cfg.load_config(yaml_path=path, include_carb=False)
    # Overrides
    assert d.annotators["rgb"] is False
    assert d.annotators["instance_segmentation"] is True
    # Defaults preserved for unmentioned keys
    assert d.annotators["semantic_segmentation"] is True
    assert d.annotators["bounding_box_2d_tight"] is True


def test_legacy_asset_folder_key_maps_to_asset_root_folder(tmp_path):
    path = _write_yaml(tmp_path, """
        asset_folder: "/old/style/path"
    """)
    d = cfg.load_config(yaml_path=path, include_carb=False)
    assert d.asset_root_folder == "/old/style/path"


def test_malformed_yaml_falls_back_to_defaults(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("{not yaml: [")
    d = cfg.load_config(yaml_path=str(path), include_carb=False)
    assert d.camera_path == "/World/BLV_Camera"  # unchanged default


def test_missing_yaml_file_uses_defaults():
    d = cfg.load_config(
        yaml_path="/nonexistent/path/config.yaml", include_carb=False
    )
    assert d.camera_path == "/World/BLV_Camera"


def test_resolution_property_returns_tuple(tmp_path):
    d = cfg.load_config(yaml_path="", include_carb=False)
    assert d.resolution == (1280, 720)
