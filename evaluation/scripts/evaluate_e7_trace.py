"""Score immutable, separately source-validated traces with one frozen Judge method.

Cross-version comparison artifacts are explicitly non-formal. Original Runtime/Rule
artifacts are never rewritten to pretend they belong to the current protocol.
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
from learning_agent_eval.active_judge import (
    OpenAICompatibleHy3JudgeProviderV3,
    _evaluate_one_v3,
)
from learning_agent_eval.canonical import canonical_json_bytes, sha256_digest
from learning_agent_eval.integrity import (
    decision_episode_digest,
    rule_result_digest,
    judge_reference_digest,
    artifact_manifest_digest,
)
from learning_agent_eval.runtime_metadata import (
    git_worktree_clean,
    dependency_lock_sha256,
    dependency_environment_reason_codes,
)
from learning_agent_eval.e3_io import current_git_commit


def verify_bundle(terminal, episode, rule, reference, runtime, rules):
    """Join immutable evidence, including self-consistent but substituted objects."""
    identity = terminal["artifact_id"]
    episode_hash = decision_episode_digest(episode)
    rule_hash = rule_result_digest(rule)
    reference_hash = judge_reference_digest(reference)
    conditions = [
        terminal["artifact_sha256"]
        == episode["provenance"]["episode_sha256"]
        == episode_hash,
        terminal["case_spec_sha256"]
        == episode["case_spec_sha256"]
        == rule["case_spec_sha256"],
        identity == episode["episode_id"] == rule["episode_id"],
        terminal["track"] == episode["track"],
        episode["judge_reference_sha256"]
        == rule["judge_reference_sha256"]
        == reference["reference_sha256"]
        == reference_hash,
        rule["result_sha256"] == rules["rule_result_digests"][identity] == rule_hash,
        rule["episode_sha256"]
        == rules["input_episode_digests"][identity]
        == episode_hash,
        rules["input_reference_digests"][identity] == reference_hash,
        rule["input_runtime_manifest_sha256"] == runtime["manifest_sha256"],
        terminal["runtime_run_id"]
        == episode["provenance"]["runtime_run_id"]
        == runtime["runtime_run_id"]
        == rule["runtime_run_id"],
    ]
    for key in (
        "evaluation_protocol_release_id",
        "evaluation_protocol_release_sha256",
        "benchmark_release_id",
        "benchmark_release_sha256",
    ):
        conditions.append(
            episode["provenance"][key] == runtime[key] == rule[key] == rules[key]
        )
    if not all(conditions):
        raise ValueError("trace_bundle_linkage_invalid")


def score(run: Path, output: Path, ledger: Path, ids: set[str] | None = None):
    if output.exists():
        raise FileExistsError(output)
    if not git_worktree_clean() or dependency_environment_reason_codes():
        raise ValueError(
            "clean frozen source and locked dependency environment required"
        )
    runtime = json.loads((run / "runtime/run-manifest.json").read_text())
    rules = json.loads((run / "rules/rule-manifest.json").read_text())
    assert rules["input_runtime_manifest_sha256"] == runtime["manifest_sha256"]
    assert artifact_manifest_digest(runtime) == runtime["manifest_sha256"]
    assert artifact_manifest_digest(rules) == rules["manifest_sha256"]
    output.mkdir(parents=True)
    provider = OpenAICompatibleHy3JudgeProviderV3(
        budget_ledger=ledger, scope=output.name
    )
    rows = []
    for terminal in runtime["terminals"]:
        if ids is not None and terminal["case_id"] not in ids:
            continue
        row = {**terminal, "comparison_status": "runtime_failure", "judge_result": None}
        if terminal["terminal_kind"] == "episode":
            identity = terminal["artifact_id"]
            e = json.loads((run / "runtime/episodes" / f"{identity}.json").read_text())
            rule = json.loads((run / "rules/rules" / f"{identity}.json").read_text())
            ref = json.loads(
                (run / "runtime/judge-references" / f"{identity}.json").read_text()
            )
            assert decision_episode_digest(e) == e["provenance"]["episode_sha256"]
            assert rule_result_digest(rule) == rule["result_sha256"]
            assert judge_reference_digest(ref) == ref["reference_sha256"]
            verify_bundle(terminal, e, rule, ref, runtime, rules)
            result, repaired = _evaluate_one_v3(
                episode=e,
                rule_result=rule,
                reference=ref,
                rule_manifest=rules,
                judge_mode="real",
                provider=provider,
                selection_mode="adhoc_filter",
                git_commit=current_git_commit(),
                worktree_clean=True,
                dependency_digest=dependency_lock_sha256(),
                dependency_lock_verified=True,
                attempt_log_path=output / "attempts.jsonl",
            )
            (output / f"{identity}.json").write_bytes(canonical_json_bytes(result))
            row.update(
                comparison_status=result["status"],
                judge_result=f"{identity}.json",
                repaired=repaired,
                episode_sha256=e["provenance"]["episode_sha256"],
                judge_result_sha256=result["result_sha256"],
            )
        rows.append(row)
        (output / "comparison-manifest.json").write_bytes(
            canonical_json_bytes(
                dict(
                    version="e7-trace-rescoring-v1",
                    formal_capability_result=False,
                    purpose="cross-version comparison; source protocol retained, current Judge separately attributed",
                    source_runtime_manifest_sha256=runtime["manifest_sha256"],
                    judge_source_commit=current_git_commit(),
                    rows=rows,
                )
            )
        )
        print(terminal["case_id"], row["comparison_status"], flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--budget-ledger", type=Path, required=True)
    p.add_argument("--case-id", action="append")
    a = p.parse_args()
    score(a.run, a.output, a.budget_ledger, set(a.case_id) if a.case_id else None)
