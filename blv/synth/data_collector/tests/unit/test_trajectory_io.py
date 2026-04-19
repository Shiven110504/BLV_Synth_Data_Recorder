"""Tests for :mod:`blv.synth.data_collector.backend.trajectory_io`."""

from __future__ import annotations

import json

from blv.synth.data_collector.backend import trajectory_io as tio


def test_build_trajectory_payload_has_expected_shape():
    frames = [
        {"frame": 0, "position": [1.0, 2.0, 3.0], "rotation": [0.0, 0.0, 0.0]},
        {"frame": 1, "position": [1.1, 2.0, 3.0], "rotation": [0.0, 1.0, 0.0]},
    ]
    data = tio.build_trajectory_payload(
        name="run_1",
        environment="hospital",
        camera_path="/World/BLV_Camera",
        frames=frames,
        fps=60,
        created="2026-04-06T00:00:00",
    )
    assert data["version"] == "1.0"
    assert data["name"] == "run_1"
    assert data["environment"] == "hospital"
    assert data["camera_path"] == "/World/BLV_Camera"
    assert data["fps"] == 60
    assert data["frame_count"] == 2
    assert data["created"] == "2026-04-06T00:00:00"
    assert data["frames"] == frames


def test_write_then_read_trajectory_roundtrip(tmp_path):
    frames = [
        {"frame": 0, "position": [1.0, 2.0, 3.0], "rotation": [0.0, 0.0, 0.0]},
    ]
    data = tio.build_trajectory_payload(
        "t1", "env", "/cam", frames, created="2026-01-01T00:00:00"
    )
    target = tmp_path / "sub" / "t1.json"
    path = tio.write_trajectory_json(data, str(target))
    assert path == str(target)
    assert target.is_file()

    loaded = tio.read_trajectory_json(str(target))
    assert loaded == data


def test_list_trajectory_files_sorts_and_filters(tmp_path):
    (tmp_path / "zulu.json").write_text("{}")
    (tmp_path / "alpha.json").write_text("{}")
    (tmp_path / "README.md").write_text("ignore")

    files = tio.list_trajectory_files(str(tmp_path))
    names = [f.split("/")[-1] for f in files]
    assert names == ["alpha.json", "zulu.json"]


def test_list_trajectory_names_filters_extensions(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "c.json").write_text("{}")

    assert tio.list_trajectory_names(str(tmp_path)) == ["a.json", "c.json"]


def test_list_trajectory_files_missing_dir_returns_empty():
    assert tio.list_trajectory_files("/nonexistent/path") == []
    assert tio.list_trajectory_names("") == []


def test_list_trajectory_info_reports_frame_count(tmp_path):
    data = tio.build_trajectory_payload(
        "t", "env", "/cam",
        [{"frame": 0, "position": [0, 0, 0], "rotation": [0, 0, 0]}] * 5,
        created="2026-01-01T00:00:00",
    )
    tio.write_trajectory_json(data, str(tmp_path / "t.json"))
    (tmp_path / "bad.json").write_text("{not json")

    info = tio.list_trajectory_info(str(tmp_path))
    by_name = {item["name"]: item for item in info}
    assert by_name["t.json"]["frame_count"] == 5
    assert by_name["bad.json"]["frame_count"] == -1
