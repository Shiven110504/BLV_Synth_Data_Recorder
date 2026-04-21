"""Parser-level tests for the ``blv-collect`` CLI.

We don't boot Isaac here, so the tests cover only argparse wiring: the
three subcommands exist, ``run`` was removed, and each subcommand
accepts the flags documented in the README.
"""

from __future__ import annotations

import pytest

from blv.synth.data_collector.cli.__main__ import build_parser


def test_subcommands_registered():
    parser = build_parser()
    # argparse stores subparser choices on the action object.
    (action,) = [
        a for a in parser._actions
        if a.__class__.__name__ == "_SubParsersAction"
    ]
    assert set(action.choices) == {"list", "record-all", "collect-all"}


def test_run_subcommand_removed():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--env", "x", "--class", "y",
                           "--trajectory", "t.json"])


def test_record_all_accepts_config_only():
    parser = build_parser()
    args = parser.parse_args(["record-all", "--config", "cfg.yaml"])
    assert args.command == "record-all"
    assert args.config == "cfg.yaml"
    assert args.env is None
    assert args.class_name is None
    assert args.location is None
    assert args.frame_step is None


def test_record_all_overrides():
    parser = build_parser()
    args = parser.parse_args([
        "record-all",
        "--config", "cfg.yaml",
        "--env", "hotel_corridor",
        "--class", "elevator",
        "--location", "entrance",
        "--frame-step", "25",
    ])
    assert args.env == "hotel_corridor"
    assert args.class_name == "elevator"
    assert args.location == "entrance"
    assert args.frame_step == 25


def test_collect_all_defaults():
    parser = build_parser()
    args = parser.parse_args(["collect-all", "--config", "cfg.yaml"])
    assert args.command == "collect-all"
    assert args.on_error == "skip"
    assert args.frame_step is None
    assert args.class_name is None


def test_collect_all_on_error_choices():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["collect-all", "--on-error", "bogus"])
