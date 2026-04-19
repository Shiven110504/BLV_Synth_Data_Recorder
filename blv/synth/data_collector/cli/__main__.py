"""``python -m blv.synth.data_collector.cli`` / ``blv-collect`` entry point."""

from __future__ import annotations

import argparse
import sys

from .commands import collect_all_cmd, list_cmd, run_cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blv-collect",
        description="BLV Synth Data Collector command-line entry point.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_cmd.add_parser(subparsers)
    run_cmd.add_parser(subparsers)
    collect_all_cmd.add_parser(subparsers)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
