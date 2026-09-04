"""Command-line entry point for versioned evaluation control planes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .aggregate import AggregateEvaluationError, aggregate_results
from .aggregate_v2 import (
    AggregateEvaluationV2Error,
    aggregate_results_v2,
    aggregate_results_v3,
)
from .judge import JudgeEvaluationError, evaluate_judges
from .judge_v2 import JudgeEvaluationV2Error, evaluate_judges_v2, evaluate_judges_v3
from .rule_runner import RuleEvaluationError, evaluate_run_rules
from .rule_runner_v2 import (
    RuleEvaluationV2Error,
    evaluate_run_rules_v2,
    evaluate_run_rules_v3,
)
from .runner import RunAgentError, run_agent
from .runner_v3 import RunAgentV3Error, run_agent_v3, run_agent_v4
from .validator import validate_dataset


def _document_version(path: str) -> str | None:
    """Best-effort dispatch hint; selected runners still validate the document."""

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return str(document.get("schema_version")) if isinstance(document, dict) else None


def _run_manifest_version(root: str) -> str | None:
    return _document_version(str(Path(root) / "run-manifest.json"))


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
        help="run isolated CaseSpecs and export active DecisionEpisode v4 artifacts",
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
        help="run deterministic Rules over active v4 Runtime output",
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
        help="run the blind structured Judge over matched v4 and Rule v3 artifacts",
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
        help="deterministically aggregate active v4 Rule and Judge results",
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
        manifest_version = _run_manifest_version(arguments.input)
        try:
            evaluator = (
                evaluate_run_rules_v3
                if manifest_version == "runtime-run-manifest-v3"
                else evaluate_run_rules_v2
                if manifest_version == "runtime-run-manifest-v2"
                else evaluate_run_rules
            )
            summary = evaluator(
                input_path=arguments.input,
                output=arguments.output,
                episode_ids=set(arguments.episode_id) or None,
                track=arguments.track,
            )
        except (RuleEvaluationError, RuleEvaluationV2Error) as exc:
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
            f"runtime_failures={len(getattr(summary, 'runtime_failure_ids', ()))} "
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
        manifest_version = _run_manifest_version(arguments.episodes)
        try:
            evaluator = (
                evaluate_judges_v3
                if manifest_version == "runtime-run-manifest-v3"
                else evaluate_judges_v2
                if manifest_version == "runtime-run-manifest-v2"
                else evaluate_judges
            )
            summary = evaluator(
                episodes=arguments.episodes,
                rules=arguments.rules,
                output=arguments.output,
                judge_mode=arguments.judge_mode,
                allow_real_judge=arguments.allow_real_judge,
                stub_response=arguments.stub_response,
                episode_ids=set(arguments.episode_id) or None,
                track=arguments.track,
            )
        except (JudgeEvaluationError, JudgeEvaluationV2Error) as exc:
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
            f"runtime_failures={len(getattr(summary, 'runtime_failure_ids', ()))} "
            f"invalid_input={len(summary.invalid_episode_ids)} "
            f"judge_errors={len(summary.judge_error_episode_ids)} "
            f"repairs={len(summary.repair_attempted_episode_ids)} "
            f"formal_evaluation_result={str(summary.formal_evaluation_result).lower()}"
        )
        if summary.invalid_episode_ids:
            return 1
        return 2 if summary.judge_error_episode_ids else 0
    if arguments.command == "aggregate-results":
        manifest_version = _run_manifest_version(arguments.judges)
        try:
            aggregator = (
                aggregate_results_v3
                if manifest_version == "judge-run-manifest-v3"
                else aggregate_results_v2
                if manifest_version == "judge-run-manifest-v2"
                else aggregate_results
            )
            summary = aggregator(
                episodes=arguments.episodes,
                rules=arguments.rules,
                judges=arguments.judges,
                output=arguments.output,
                episode_ids=set(arguments.episode_id) or None,
                track=arguments.track,
            )
        except (AggregateEvaluationError, AggregateEvaluationV2Error) as exc:
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
            f"runtime_failures={len(getattr(summary, 'runtime_failure_ids', ()))} "
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
    manifest_version = _document_version(arguments.manifest)
    try:
        runner = (
            run_agent_v4
            if manifest_version == "case-suite-manifest-v2"
            else run_agent_v3
            if manifest_version == "case-suite-manifest-v1"
            else run_agent
        )
        summary = runner(
            dataset=arguments.dataset,
            manifest=arguments.manifest,
            output=arguments.output,
            episode_ids=set(arguments.episode_id) or None,
            track=arguments.track,
            model_mode=arguments.model_mode,
            allow_real_model=arguments.allow_real_model,
        )
    except (RunAgentError, RunAgentV3Error) as exc:
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
        "run_agent_ok "
        f"episodes={len(summary.episode_ids)} tracks={track_counts} "
        f"runtime_failures={len(getattr(summary, 'failure_ids', ()))} "
        f"invocation_mode={summary.invocation_mode} "
        f"formal_evaluation_result="
        f"{str(getattr(summary, 'formal_evaluation_result', False)).lower()} "
        f"evaluation_status="
        f"{'formal_model_evaluation' if getattr(summary, 'formal_evaluation_result', False) else 'not_a_formal_model_evaluation'}"
    )
    return 2 if getattr(summary, "failure_ids", ()) else 0
