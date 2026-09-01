"""Offline, side-effect-free protocol tools for Learning Agent evaluation."""

from .canonical import canonical_json, canonical_json_bytes, sha256_digest
from .errors import DatasetStats, ValidationIssue, ValidationReport
from .integrity import (
    aggregate_result_digest,
    artifact_manifest_digest,
    context_summary_digest,
    decision_episode_digest,
    environment_manifest_digest,
    episode_completeness_digest,
    integrity_result_digest,
    judge_result_digest,
    oracle_envelope_digest,
    rule_result_digest,
    snapshot_entity_digest,
    state_delta_digest,
    state_snapshot_digest,
)
from .validator import resolve_evidence_path, validate_dataset, validate_episode

__all__ = [
    "DatasetStats",
    "ValidationIssue",
    "ValidationReport",
    "aggregate_result_digest",
    "artifact_manifest_digest",
    "canonical_json",
    "canonical_json_bytes",
    "context_summary_digest",
    "decision_episode_digest",
    "environment_manifest_digest",
    "episode_completeness_digest",
    "integrity_result_digest",
    "judge_result_digest",
    "oracle_envelope_digest",
    "resolve_evidence_path",
    "rule_result_digest",
    "sha256_digest",
    "snapshot_entity_digest",
    "state_delta_digest",
    "state_snapshot_digest",
    "validate_dataset",
    "validate_episode",
]
