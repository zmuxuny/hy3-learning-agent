"""Deterministically expose JSON Schema documents from the source models."""

from __future__ import annotations

from typing import Any

from .models import SCHEMA_MODELS
from .release_governance import ACTIVE_PROTOCOL_VERSION

ACTIVE_SCHEMA_RELATIVE_ROOT = f"evaluation/schemas/protocol-{ACTIVE_PROTOCOL_VERSION}"

SCHEMA_FILENAMES = {
    "decision-episode-v1": "decision-episode-v1.schema.json",
    "decision-episode-v2": "decision-episode-v2.schema.json",
    "acceptable-action-envelope-v1": "acceptable-action-envelope-v1.schema.json",
    "environment-manifest-v1": "environment-manifest-v1.schema.json",
    "rule-result-v1": "rule-result-v1.schema.json",
    "judge-result-v1": "judge-result-v1.schema.json",
    "judge-run-manifest-v1": "judge-run-manifest-v1.schema.json",
    "aggregate-result-v1": "aggregate-result-v1.schema.json",
    "aggregate-track-result-v1": "aggregate-track-result-v1.schema.json",
    "aggregate-run-manifest-v1": "aggregate-run-manifest-v1.schema.json",
    "case-spec-v1": "case-spec-v1.schema.json",
    "judge-reference-v1": "judge-reference-v1.schema.json",
    "provider-attestation-v1": "provider-attestation-v1.schema.json",
    "environment-manifest-v2": "environment-manifest-v2.schema.json",
    "decision-episode-v3": "decision-episode-v3.schema.json",
    "runtime-failure-v1": "runtime-failure-v1.schema.json",
    "runtime-run-manifest-v2": "runtime-run-manifest-v2.schema.json",
    "case-suite-manifest-v1": "case-suite-manifest-v1.schema.json",
    "rule-result-v2": "rule-result-v2.schema.json",
    "rule-run-manifest-v2": "rule-run-manifest-v2.schema.json",
    "judge-result-v2": "judge-result-v2.schema.json",
    "judge-run-manifest-v2": "judge-run-manifest-v2.schema.json",
    "aggregate-result-v2": "aggregate-result-v2.schema.json",
    "aggregate-track-result-v2": "aggregate-track-result-v2.schema.json",
    "aggregate-run-manifest-v2": "aggregate-run-manifest-v2.schema.json",
    "case-spec-v2": "case-spec-v2.schema.json",
    "judge-reference-v2": "judge-reference-v2.schema.json",
    "decision-episode-v4": "decision-episode-v4.schema.json",
    "runtime-failure-v2": "runtime-failure-v2.schema.json",
    "case-suite-manifest-v2": "case-suite-manifest-v2.schema.json",
    "runtime-run-manifest-v3": "runtime-run-manifest-v3.schema.json",
    "rule-result-v3": "rule-result-v3.schema.json",
    "rule-run-manifest-v3": "rule-run-manifest-v3.schema.json",
    "judge-result-v3": "judge-result-v3.schema.json",
    "judge-run-manifest-v3": "judge-run-manifest-v3.schema.json",
    "aggregate-result-v3": "aggregate-result-v3.schema.json",
    "aggregate-track-result-v3": "aggregate-track-result-v3.schema.json",
    "aggregate-run-manifest-v3": "aggregate-run-manifest-v3.schema.json",
    "evaluation-protocol-release-v1": "evaluation-protocol-release-v1.schema.json",
    "benchmark-release-manifest-v1": "benchmark-release-manifest-v1.schema.json",
    "source-bundle-manifest-v1": "source-bundle-manifest-v1.schema.json",
    "schema-lock-manifest-v1": "schema-lock-manifest-v1.schema.json",
    "trusted-benchmark-registry-v1": "trusted-benchmark-registry-v1.schema.json",
    "action-declaration-protocol-v2": "action-declaration-protocol-v2.schema.json",
}


def schema_documents() -> dict[str, dict[str, Any]]:
    """Return each committed contract Schema keyed by its stable file name."""

    documents = {
        SCHEMA_FILENAMES[version]: model.model_json_schema(mode="validation")
        for version, model in SCHEMA_MODELS.items()
    }
    for filename, document in documents.items():
        document["$id"] = (
            document["$id"].rsplit("/", 1)[0]
            + f"/protocol-{ACTIVE_PROTOCOL_VERSION}/"
            + filename
        )
    return documents
