"""Tests for :class:`LocationManager` CRUD operations."""

from __future__ import annotations

import json

import pytest

from blv.synth.data_collector.backend.location import LocationManager


@pytest.fixture
def mgr(tmp_path):
    m = LocationManager()
    m.set_base_directory(str(tmp_path), "elevator_button", "hospital")
    # set_base_directory doesn't create the directory itself (matching
    # the original behaviour); create_location handles that on first use.
    (tmp_path / "elevator_button" / "hospital").mkdir(parents=True)
    return m


def test_validate_name_rejects_empty(mgr):
    ok, err = mgr.validate_name("")
    assert not ok and "empty" in err.lower()


def test_validate_name_rejects_bad_chars(mgr):
    ok, err = mgr.validate_name("bad name!")
    assert not ok and "invalid" in err.lower()


def test_validate_name_rejects_duplicate(mgr, tmp_path):
    (tmp_path / "elevator_button" / "hospital" / "lobby").mkdir()
    ok, err = mgr.validate_name("lobby")
    assert not ok and "already exists" in err


def test_validate_name_accepts_good_name(mgr):
    ok, err = mgr.validate_name("lobby_A")
    assert ok and err == ""


def test_create_and_load_location_roundtrip(mgr):
    t = [1.0, 2.0, 3.0]
    r = [1.0, 0.0, 0.0, 0.0]
    s = [1.0, 1.0, 1.0]

    mgr.create_location("lobby", t, r, s)

    data = mgr.load_location("lobby")
    assert data["name"] == "lobby"
    assert data["spawn_transform"]["translate"] == t
    assert data["spawn_transform"]["orient"] == r
    assert data["spawn_transform"]["scale"] == s
    assert data["version"] == "1.0"


def test_save_transform_preserves_metadata(mgr):
    mgr.create_location("lobby", [0, 0, 0], [1, 0, 0, 0], [1, 1, 1])
    data = mgr.load_location("lobby")
    original_created = data["created"]

    mgr.save_transform("lobby", [5, 6, 7], [1, 0, 0, 0], [2, 2, 2])

    data2 = mgr.load_location("lobby")
    assert data2["created"] == original_created  # preserved
    assert data2["spawn_transform"]["translate"] == [5, 6, 7]
    assert data2["spawn_transform"]["scale"] == [2, 2, 2]


def test_save_transform_rebuilds_on_corrupt_json(mgr, tmp_path):
    loc_dir = tmp_path / "elevator_button" / "hospital" / "lobby"
    loc_dir.mkdir()
    # Corrupt file
    (loc_dir / "location.json").write_text("{not json")

    mgr.save_transform("lobby", [1, 2, 3], [1, 0, 0, 0], [1, 1, 1])

    with open(loc_dir / "location.json") as fh:
        data = json.load(fh)
    assert data["spawn_transform"]["translate"] == [1, 2, 3]
    assert data["name"] == "lobby"


def test_list_locations_skips_dirs_without_json(mgr, tmp_path):
    base = tmp_path / "elevator_button" / "hospital"

    # One good, one bare dir, one dir with non-json file
    mgr.create_location("good_loc", [0, 0, 0], [1, 0, 0, 0], [1, 1, 1])
    (base / "no_json_dir").mkdir()
    (base / "wrong_file").mkdir()
    (base / "wrong_file" / "notes.txt").write_text("hi")

    assert mgr.list_locations() == ["good_loc"]


def test_delete_location_removes_dir(mgr, tmp_path):
    mgr.create_location("lobby", [0, 0, 0], [1, 0, 0, 0], [1, 1, 1])
    loc_dir = tmp_path / "elevator_button" / "hospital" / "lobby"
    assert loc_dir.is_dir()

    assert mgr.delete_location("lobby") is True
    assert not loc_dir.exists()


def test_delete_location_clears_current(mgr):
    mgr.create_location("lobby", [0, 0, 0], [1, 0, 0, 0], [1, 1, 1])
    mgr.current_location = "lobby"
    mgr.delete_location("lobby")
    assert mgr.current_location == ""


def test_delete_location_missing_returns_false(mgr):
    assert mgr.delete_location("nonexistent") is False


def test_create_location_rejects_invalid_name(mgr):
    with pytest.raises(ValueError):
        mgr.create_location("bad name!", [0, 0, 0], [1, 0, 0, 0], [1, 1, 1])
