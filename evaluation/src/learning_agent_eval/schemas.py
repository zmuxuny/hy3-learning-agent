"""Deterministically expose JSON Schema documents from the source models."""

from __future__ import annotations

from typing import Any

from .models import SCHEMA_MODELS

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
}


def schema_documents() -> dict[str, dict[str, Any]]:
    """Return each committed contract Schema keyed by its stable file name."""

    return {
        SCHEMA_FILENAMES[version]: model.model_json_schema(mode="validation")
        for version, model in SCHEMA_MODELS.items()
    }
