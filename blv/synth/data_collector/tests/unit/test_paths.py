"""Tests for :mod:`blv.synth.data_collector.backend.paths`.

These exercise purely deterministic filesystem-path derivation — they
don't require Isaac Sim / carb / omni on the PYTHONPATH, just ``pytest``
and the stubs in :mod:`conftest`.
"""

from __future__ import annotations

import os

from blv.synth.data_collector.backend import paths


def test_normalize_expands_user(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/fakehome")
    assert paths.normalize("~/data") == os.path.normpath("/tmp/fakehome/data")


def test_normalize_empty_returns_empty():
    assert paths.normalize("") == ""


def test_sanitize_folder_name_replaces_unsafe_chars():
    assert paths.sanitize_folder_name("My Asset #1") == "My_Asset__1"
    assert paths.sanitize_folder_name("weird/name") == "weird_name"
    assert paths.sanitize_folder_name("good-name_1.0") == "good-name_1.0"


def test_class_env_dir_joins_correctly(tmp_path):
    out = paths.class_env_dir(str(tmp_path), "elevator_button", "hospital")
    assert out == str(tmp_path / "elevator_button" / "hospital")


def test_location_dir_with_location(tmp_path):
    out = paths.location_dir(
        str(tmp_path), "elevator_button", "hospital", "lobby"
    )
    assert out == str(tmp_path / "elevator_button" / "hospital" / "lobby")


def test_location_dir_without_location(tmp_path):
    out = paths.location_dir(str(tmp_path), "elevator_button", "hospital", "")
    assert out == str(tmp_path / "elevator_button" / "hospital")


def test_run_dir_with_traj_stem(tmp_path):
    out = paths.run_dir(str(tmp_path), "asset_01", "traj_a")
    assert out == str(tmp_path / "asset_01" / "traj_a")


def test_run_dir_without_traj_stem(tmp_path):
    out = paths.run_dir(str(tmp_path), "asset_01")
    assert out == str(tmp_path / "asset_01")


def test_next_default_run_name_empty_dir(tmp_path):
    assert paths.next_default_run_name(str(tmp_path)) == "default_1"


def test_next_default_run_name_skips_non_numeric(tmp_path):
    (tmp_path / "default_1").mkdir()
    (tmp_path / "default_3").mkdir()
    (tmp_path / "default_foo").mkdir()
    (tmp_path / "unrelated").mkdir()
    assert paths.next_default_run_name(str(tmp_path)) == "default_4"


def test_next_default_run_name_missing_base():
    # Non-existent directory should still return default_1.
    assert paths.next_default_run_name("/nonexistent/path/xyz") == "default_1"


def test_plan_collect_all_builds_deterministic_plan(tmp_path):
    # Fake a layout:
    #   envs/{A,B}/*.usd
    #   root/cls/{A,B}/{lobby,entrance}/{location.json, trajectories/*.json}
    envs = tmp_path / "envs"
    root = tmp_path / "root"
    for env in ("A", "B"):
        (envs / env).mkdir(parents=True)
        (envs / env / f"{env}.usd").write_text("fake")

    cls = "elevator_button"
    for env in ("A", "B"):
        for loc in ("lobby", "entrance"):
            loc_dir = root / cls / env / loc
            (loc_dir / "trajectories").mkdir(parents=True)
            (loc_dir / "location.json").write_text("{}")
            (loc_dir / "trajectories" / "traj_02.json").write_text("{}")
            (loc_dir / "trajectories" / "traj_01.json").write_text("{}")

    plans = paths.plan_collect_all(str(root), cls, str(envs))
    assert [p.env_name for p in plans] == ["A", "B"]
    # Locations and trajectories should be sorted.
    for p in plans:
        assert list(p.locations.keys()) == ["entrance", "lobby"]
        for traj_list in p.locations.values():
            assert traj_list == ["traj_01.json", "traj_02.json"]


def test_plan_collect_all_skips_envs_without_locations(tmp_path):
    # Env C exists but has no location.json → it should be dropped.
    envs = tmp_path / "envs"
    root = tmp_path / "root"
    (envs / "C").mkdir(parents=True)
    (envs / "C" / "C.usd").write_text("fake")

    cls = "button"
    data_c = root / cls / "C" / "lobby"
    data_c.mkdir(parents=True)
    # Has traj but no location.json
    (data_c / "trajectories").mkdir()
    (data_c / "trajectories" / "t.json").write_text("{}")

    plans = paths.plan_collect_all(str(root), cls, str(envs))
    assert plans == []


def test_plan_collect_all_skips_env_without_usd(tmp_path):
    envs = tmp_path / "envs"
    (envs / "D").mkdir(parents=True)
    # No D.usd file
    plans = paths.plan_collect_all(str(tmp_path / "root"), "cls", str(envs))
    assert plans == []


def test_plan_totals(tmp_path):
    envs = tmp_path / "envs"
    root = tmp_path / "root"
    (envs / "A").mkdir(parents=True)
    (envs / "A" / "A.usd").write_text("fake")

    cls = "cls"
    for loc in ("lobby", "entrance"):
        loc_dir = root / cls / "A" / loc
        (loc_dir / "trajectories").mkdir(parents=True)
        (loc_dir / "location.json").write_text("{}")
        for n in range(3):
            (loc_dir / "trajectories" / f"t{n}.json").write_text("{}")

    plans = paths.plan_collect_all(str(root), cls, str(envs))
    n_envs, n_locs, n_trajs = paths.plan_totals(plans)
    assert (n_envs, n_locs, n_trajs) == (1, 2, 6)
