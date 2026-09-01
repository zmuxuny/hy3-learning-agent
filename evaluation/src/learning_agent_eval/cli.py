"""Command-line entry point for versioned evaluation control planes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .aggregate import AggregateEvaluationError, aggregate_results
from .judge import JudgeEvaluationError, evaluate_judges
from .rule_runner import RuleEvaluationError, evaluate_run_rules
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
        help="run isolated Runtime fixtures and export DecisionEpisode v2",
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
    rules = commands.add_parser(
        "evaluate-rules",
        help="run E2 completeness and deterministic Rules over v2 Runtime output",
    )
    rules.add_argument("--input", required=True)
    rules.add_argument("--output", required=True)
    rules.add_argument("--episode-id", action="append", default=[])
    rules.add_argument(
        "--track",
        choices=("planning", "intervention", "assessment", "revision"),
    )
    judge = commands.add_parser(
        "evaluate-judge",
        help="run the E3 blind structured Judge over matched v2 and Rule artifacts",
    )
    judge.add_argument("--episodes", required=True)
    judge.add_argument("--rules", required=True)
    judge.add_argument("--output", required=True)
    judge.add_argument("--episode-id", action="append", default=[])
    judge.add_argument(
        "--track",
        choices=("planning", "intervention", "assessment", "revision"),
    )
    judge.add_argument("--judge-mode", choices=("stub", "real"), default="stub")
    judge.add_argument("--allow-real-judge", action="store_true")
    judge.add_argument("--stub-response")
    aggregate = commands.add_parser(
        "aggregate-results",
        help="deterministically aggregate E3 Rule and Judge results",
    )
    aggregate.add_argument("--episodes", required=True)
    aggregate.add_argument("--rules", required=True)
    aggregate.add_argument("--judges", required=True)
    aggregate.add_argument("--output", required=True)
    aggregate.add_argument("--episode-id", action="append", default=[])
    aggregate.add_argument(
        "--track",
        choices=("planning", "intervention", "assessment", "revision"),
    )
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
    if arguments.command == "evaluate-rules":
        try:
            summary = evaluate_run_rules(
                input_path=arguments.input,
                output=arguments.output,
                episode_ids=set(arguments.episode_id) or None,
                track=arguments.track,
            )
        except RuleEvaluationError as exc:
            print(
                json.dumps(exc.as_dict(), sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
            )
            return 1
        track_counts = ",".join(
            f"{track}:{summary.tracks.count(track)}"
            for track in sorted(set(summary.tracks))
        )
        print(
            "evaluate_rules_ok "
            f"episodes={len(summary.episode_ids)} tracks={track_counts} "
            f"failed_episodes={len(summary.failed_episode_ids)} "
            f"invalid_input_episodes={len(summary.invalid_episode_ids)} "
            f"hard_gate_episodes={len(summary.hard_gate_episode_ids)} "
            f"formal_evaluation_result={str(summary.formal_evaluation_result).lower()}"
        )
        if summary.invalid_episode_ids:
            return 1
        if summary.hard_gate_episode_ids:
            return 2
        return 3 if summary.failed_episode_ids else 0
    if arguments.command == "evaluate-judge":
        try:
            summary = evaluate_judges(
                episodes=arguments.episodes,
                rules=arguments.rules,
                output=arguments.output,
                judge_mode=arguments.judge_mode,
                allow_real_judge=arguments.allow_real_judge,
                stub_response=arguments.stub_response,
                episode_ids=set(arguments.episode_id) or None,
                track=arguments.track,
            )
        except JudgeEvaluationError as exc:
            print(
                json.dumps(exc.as_dict(), sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
            )
            return 1
        track_counts = ",".join(
            f"{track}:{summary.tracks.count(track)}"
            for track in sorted(set(summary.tracks))
        )
        complete = (
            len(summary.episode_ids)
            - len(summary.invalid_episode_ids)
            - len(summary.judge_error_episode_ids)
        )
        print(
            "evaluate_judge_ok "
            f"episodes={len(summary.episode_ids)} tracks={track_counts} "
            f"judge_mode={arguments.judge_mode} complete={complete} "
            f"invalid_input={len(summary.invalid_episode_ids)} "
            f"judge_errors={len(summary.judge_error_episode_ids)} "
            f"repairs={len(summary.repair_attempted_episode_ids)} "
            f"formal_evaluation_result={str(summary.formal_evaluation_result).lower()}"
        )
        if summary.invalid_episode_ids:
            return 1
        return 2 if summary.judge_error_episode_ids else 0
    if arguments.command == "aggregate-results":
        try:
            summary = aggregate_results(
                episodes=arguments.episodes,
                rules=arguments.rules,
                judges=arguments.judges,
                output=arguments.output,
                episode_ids=set(arguments.episode_id) or None,
                track=arguments.track,
            )
        except AggregateEvaluationError as exc:
            print(
                json.dumps(exc.as_dict(), sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
            )
            return 1
        track_counts = ",".join(
            f"{track}:{summary.tracks.count(track)}"
            for track in sorted(set(summary.tracks))
        )
        print(
            "aggregate_results_ok "
            f"episodes={len(summary.episode_ids)} tracks={track_counts} "
            f"failed={len(summary.failed_episode_ids)} "
            f"invalid_input={len(summary.invalid_episode_ids)} "
            f"judge_errors={len(summary.judge_error_episode_ids)} "
            f"formal_evaluation_result={str(summary.formal_evaluation_result).lower()}"
        )
        if summary.invalid_episode_ids:
            return 1
        if summary.judge_error_episode_ids:
            return 2
        return 3 if summary.failed_episode_ids else 0
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
