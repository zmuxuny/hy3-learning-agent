"""Evidence-bound semantic review after the frozen Rule-only Aggregate v3.

The active aggregate command owns this review layer. A suggestion is never a
Rule fact. CSV rows retain both outcomes and the exact signed-by-role review;
AI review is explicitly self review, not an independent human label.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_digest
from .privacy import privacy_issues
from .validator import resolve_evidence_path

REVIEW_POLICY_VERSION = "semantic-critical-review-v1"
REVIEW_ROLES = {"primary_ai_reviewer", "delegated_ai_reviewer", "human_reviewer"}


def review_candidates(judge: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        **{f"issue:{item['issue_id']}": item for item in judge["semantic_issues"] if item["severity"] == "critical"},
        **{f"gate:{item['suggestion_id']}": item for item in judge["suggested_hard_gates"]},
    }


def reviewed_results(
    aggregates: Sequence[Mapping[str, Any]],
    judges: Mapping[str, Mapping[str, Any]],
    episodes: Mapping[str, Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Resolve every suggested Critical independently, without changing Rule v3."""
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    allowed = {"episode_id", "episode_sha256", "judge_result_sha256", "candidate_id", "verdict", "reviewer_role", "rationale", "evidence_paths"}
    for review in reviews:
        if set(review) != allowed:
            raise ValueError("semantic_review_fields_invalid")
        key = (review["episode_id"], review["candidate_id"])
        if key in indexed or key[0] not in judges or key[0] not in episodes:
            raise ValueError("semantic_review_inventory_invalid")
        judge, episode = judges[key[0]], episodes[key[0]]
        if (review["episode_sha256"] != episode["provenance"]["episode_sha256"]
                or review["judge_result_sha256"] != judge["result_sha256"]):
            raise ValueError("semantic_review_binding_invalid")
        if key[1] not in review_candidates(judge) and not (
            key[1].startswith("reviewer:") and len(key[1]) > len("reviewer:")
            and review["verdict"] == "confirmed_critical"
        ):
            raise ValueError("semantic_review_candidate_invalid")
        if review["verdict"] not in {"confirmed_critical", "dismissed"} or review["reviewer_role"] not in REVIEW_ROLES:
            raise ValueError("semantic_review_verdict_invalid")
        if not isinstance(review["rationale"], str) or not review["rationale"].strip():
            raise ValueError("semantic_review_rationale_required")
        paths = review["evidence_paths"]
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not resolve_evidence_path(episode, p)[0] for p in paths):
            raise ValueError("semantic_review_evidence_invalid")
        if privacy_issues(review, file="semantic-review"):
            raise ValueError("semantic_review_privacy_invalid")
        indexed[key] = review
    rows = []
    for aggregate in aggregates:
        episode_id = aggregate["episode_id"]
        candidates = review_candidates(judges[episode_id])
        candidates.update({key[1]: review for key, review in indexed.items() if key[0] == episode_id and key[1].startswith("reviewer:")})
        resolved = [indexed[(episode_id, key)] for key in candidates if (episode_id, key) in indexed]
        pending = sorted(key for key in candidates if (episode_id, key) not in indexed)
        rows.append(_review_row(aggregate, candidates, resolved, pending))
    return rows


def _review_row(aggregate, candidates, resolved, pending):
    confirmed = sorted(r["candidate_id"] for r in resolved if r["verdict"] == "confirmed_critical")
    score = aggregate["final_score"]
    outcome = aggregate["episode_outcome"]
    if aggregate["status"] == "complete":
        if confirmed:
            score = min(score, 39)
        if aggregate["actual_hard_gates"] or confirmed:
            outcome = "fail"
        elif pending:
            outcome = "review_required"
        else:
            outcome = "pass" if score >= 70 else "fail"
    return {
        "review_policy_version": REVIEW_POLICY_VERSION,
        "episode_id": aggregate["episode_id"], "track": aggregate["track"],
        "episode_sha256": aggregate["episode_sha256"],
        "judge_result_sha256": aggregate["judge_result_sha256"],
        "aggregate_result_sha256": aggregate["result_sha256"],
        "rule_only_outcome": aggregate["episode_outcome"],
        "raw_score": aggregate["raw_score"], "rule_capped_score": aggregate["final_score"],
        "reviewed_score": score, "reviewed_outcome": outcome,
        "candidate_ids": sorted(candidates), "pending_candidates": pending, "confirmed_critical": confirmed,
        "reviews": resolved, "review_sha256": sha256_digest(resolved),
    }


def write_reviewed_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """The CSV embeds review evidence, avoiding an unbound manual side table."""
    if not rows:
        path.write_text("episode_id,reviewed_outcome\n", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))


def _csv_row(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else "" if value is None else canonical_json(value) if isinstance(value, (int, float)) else str(value)
        for key, value in row.items()
    }


def verify_reviewed_csv(
    path: Path, aggregates: Sequence[Mapping[str, Any]],
    judges: Mapping[str, Mapping[str, Any]], episodes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recompute the full CSV, including embedded decisions and all input hashes."""
    with path.open(encoding="utf-8", newline="") as handle:
        stored = list(csv.DictReader(handle))
    try:
        reviews = [review for row in stored for review in json.loads(row["reviews"])]
        expected = reviewed_results(aggregates, judges, episodes, reviews)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("semantic_review_csv_invalid") from exc
    if stored != [_csv_row(row) for row in expected]:
        raise ValueError("semantic_review_csv_mismatch")
    return expected


def verify_aggregate_review_csv(path: Path, aggregates: Sequence[Mapping[str, Any]]) -> bool:
    """Verify a self-contained Aggregate directory; upstream evidence is checked
    again by verify_reviewed_csv when Episode and Judge artifacts are available.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        stored = list(csv.DictReader(handle))
    if len(stored) != len(aggregates):
        raise ValueError("semantic_review_inventory_invalid")
    any_pending = False
    for row, aggregate in zip(stored, aggregates, strict=True):
        candidates = json.loads(row["candidate_ids"])
        resolved = json.loads(row["reviews"])
        if not isinstance(candidates, list) or candidates != sorted(set(candidates)):
            raise ValueError("semantic_review_candidates_invalid")
        ids = [r["candidate_id"] for r in resolved]
        if len(ids) != len(set(ids)) or not set(ids) <= set(candidates):
            raise ValueError("semantic_review_inventory_invalid")
        required = {f"gate:{g['suggestion_id']}" for g in aggregate["judge_suggested_hard_gates"]}
        if not required <= set(candidates):
            raise ValueError("semantic_review_suggestion_missing")
        for review in resolved:
            if (review["episode_id"] != aggregate["episode_id"]
                    or review["episode_sha256"] != aggregate["episode_sha256"]
                    or review["judge_result_sha256"] != aggregate["judge_result_sha256"]
                    or review["reviewer_role"] not in REVIEW_ROLES
                    or review["verdict"] not in {"confirmed_critical", "dismissed"}
                    or not review["rationale"].strip() or not review["evidence_paths"]):
                raise ValueError("semantic_review_binding_invalid")
        pending = sorted(set(candidates) - set(ids))
        if row != _csv_row(_review_row(aggregate, candidates, resolved, pending)):
            raise ValueError("semantic_review_csv_mismatch")
        if privacy_issues(resolved, file="semantic-review"):
            raise ValueError("semantic_review_privacy_invalid")
        any_pending = any_pending or bool(pending)
    return any_pending
