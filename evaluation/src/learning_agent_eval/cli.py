"""Command-line entry point for E0 validation and E1 isolated Runtime Mini runs."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .runner import RunAgentError, run_agent
from .validator import validate_dataset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m learning_agent_eval")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate-dataset",
        help="recursively validate versioned evaluation JSON",
    )
    validate.add_argument("--dataset", required=True)
    run = commands.add_parser(
        "run-agent",
        help="run isolated E1 Runtime Mini fixtures",
    )
    run.add_argument("--dataset", required=True)
    run.add_argument("--manifest", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--episode-id", action="append", default=[])
    run.add_argument(
        "--track",
        choices=("planning", "intervention", "assessment", "revision"),
    )
    run.add_argument("--model-mode", choices=("stub", "real"), default="stub")
    run.add_argument("--allow-real-model", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process status without raising on data errors."""

    arguments = _parser().parse_args(argv)
    if arguments.command == "validate-dataset":
        report = validate_dataset(arguments.dataset)
        if not report.ok:
            print(f"dataset_invalid errors={len(report.issues)}", file=sys.stderr)
            for issue in report.issues:
                print(issue.render(), file=sys.stderr)
            return 1
        counts = ",".join(f"{track}:{count}" for track, count in report.stats.by_track)
        print(f"dataset_valid episodes={report.stats.episodes} tracks={counts}")
        return 0
    try:
        summary = run_agent(
            dataset=arguments.dataset,
            manifest=arguments.manifest,
            output=arguments.output,
            episode_ids=set(arguments.episode_id) or None,
            track=arguments.track,
            model_mode=arguments.model_mode,
            allow_real_model=arguments.allow_real_model,
        )
    except RunAgentError as exc:
        print(json.dumps(exc.as_dict(), sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1
    track_counts = ",".join(
        f"{track}:{summary.tracks.count(track)}" for track in sorted(set(summary.tracks))
    )
    print(
        "run_agent_ok "
        f"episodes={len(summary.episode_ids)} tracks={track_counts} "
        f"invocation_mode={summary.invocation_mode} "
        "formal_evaluation_result=false "
        "evaluation_status=not_a_formal_model_evaluation"
    )
    return 0
