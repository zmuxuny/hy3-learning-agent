"""Protocol-eligibility predicates shared by active artifact builders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROHIBITED_ISOLATION_COUNTERS = (
    "network_calls",
    "smtp_calls",
    "smtp_ssl_calls",
    "web_push_calls",
    "imap_calls",
    "imap_ssl_calls",
    "prohibited_file_access",
    "outside_sqlite_access",
    "subprocess_calls",
    "published_sqlite_files",
)


def isolation_evidence_protocol_eligible(
    evidence: Mapping[str, Any] | None,
) -> bool:
    """Return whether observed isolation facts satisfy the active protocol."""

    if evidence is None:
        return False
    return bool(
        evidence.get("temporary_database") is True
        and evidence.get("database_inside_worker_root") is True
        and evidence.get("env_file_read") is False
        and evidence.get("repository_runtime_data_access") is False
        and evidence.get("background_services_started") is False
        and evidence.get("routing_material_exported") is False
        and all(evidence.get(field) == 0 for field in PROHIBITED_ISOLATION_COUNTERS)
    )
