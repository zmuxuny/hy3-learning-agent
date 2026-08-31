"""Command-line entry point for the E0 evaluation package."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .validator import validate_dataset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m learning_agent_eval")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate-dataset",
        help="recursively validate versioned evaluation JSON",
    )
    validate.add_argument("--dataset", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process status without raising on data errors."""

    arguments = _parser().parse_args(argv)
    if arguments.command != "validate-dataset":  # pragma: no cover - argparse owns choices
        return 2
    report = validate_dataset(arguments.dataset)
    if not report.ok:
        print(f"dataset_invalid errors={len(report.issues)}", file=sys.stderr)
        for issue in report.issues:
            print(issue.render(), file=sys.stderr)
        return 1
    counts = ",".join(f"{track}:{count}" for track, count in report.stats.by_track)
    print(f"dataset_valid episodes={report.stats.episodes} tracks={counts}")
    return 0
