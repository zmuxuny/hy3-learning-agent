from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from learning_agent_eval import (
    canonical_json_bytes,
    decision_episode_digest,
    environment_manifest_digest,
    episode_completeness_digest,
    resolve_evidence_path,
    sha256_digest,
    state_snapshot_digest,
    validate_dataset,
    validate_episode,
)
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.deltas import build_state_delta
from learning_agent_eval.exporter import (
    _durable_status,
    _effects_and_action,
    _execution_status,
    _guard_decisions,
    _guard_facts,
    _trace,
)
from learning_agent_eval.integrity import snapshot_entity_digest
from learning_agent_eval.models import DecisionEpisodeV2, RuleResultV1
from learning_agent_eval.normalizers import (
    IdentityCandidate,
    NormalizationError,
    StableIdentityRegistry,
    normalize_json,
    normalize_rfc3339,
)
from learning_agent_eval.privacy import privacy_issues
from learning_agent_eval.rule_runner import (
    RuleEvaluationError,
    evaluate_run_rules,
)
from learning_agent_eval.rules import evaluate_rules
from learning_agent_eval.runner import RunAgentSummary, run_agent
from learning_agent_eval.snapshots import normalize_reference_fields
from learning_agent_eval.worker import _execute

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"
MANIFEST = DATASET / "manifests" / "e1-mini-stub.json"


@pytest.fixture(scope="session")
def e2_batch(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, RunAgentSummary]:
    root = tmp_path_factory.mktemp("e2-batch")
    output = root / "runtime"
    return output, run_agent(dataset=DATASET, manifest=MANIFEST, output=output)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _delete_evidence_path(document: dict[str, Any], path: str) -> None:
    current: Any = document
    segments = path.split(".")
    for segment in segments[:-1]:
        key, bracket, raw_index = segment.partition("[")
        current = current[key]
        if bracket:
            current = current[int(raw_index.removesuffix("]"))]
    key, bracket, raw_index = segments[-1].partition("[")
    if bracket:
        del current[key][int(raw_index.removesuffix("]"))]
    else:
        del current[key]


def _entity(
    logical_id: str, entity_type: str, data: dict[str, Any], ordinal: int
) -> dict[str, Any]:
    entity = {
        "logical_id": logical_id,
        "entity_type": entity_type,
        "ordinal": ordinal,
        "source": "database",
        "scope_ref": None,
        "data": data,
        "entity_sha256": "0" * 64,
    }
    entity["entity_sha256"] = snapshot_entity_digest(entity)
    return entity


def _snapshot(entities: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot = {
        "collector_version": "e2-snapshot-collector-v1",
        "entity_types": sorted({item["entity_type"] for item in entities}),
        "field_allowlist_sha256": "a" * 64,
        "capture_status": "complete",
        "captured_at": "2026-08-31T00:00:00.000000Z",
        "logical_entities": entities,
        "context": {
            "public_summary": "Public synthetic state.",
            "source_refs": [entities[0]["logical_id"]],
            "context_sha256": "b" * 64,
        },
        "error_codes": [],
        "snapshot_sha256": "0" * 64,
    }
    snapshot["snapshot_sha256"] = state_snapshot_digest(snapshot)
    return snapshot


def _rehash_episode(episode: dict[str, Any]) -> None:
    for root in ("state_before", "state_after"):
        for entity in episode[root]["logical_entities"]:
            entity["entity_sha256"] = snapshot_entity_digest(entity)
        episode[root]["snapshot_sha256"] = state_snapshot_digest(episode[root])
    episode["state_delta"] = build_state_delta(
        episode["state_before"], episode["state_after"]
    )
    episode["completeness"]["completeness_sha256"] = episode_completeness_digest(
        episode["completeness"]
    )
    episode["provenance"]["episode_sha256"] = decision_episode_digest(episode)


def _operation(
    ordinal: int,
    *,
    target: str,
    before: int,
    after: int,
) -> dict[str, Any]:
    return _entity(
        f"operation:test:{ordinal:03d}",
        "operation",
        {
            "run_ref": "agent_run:test:001",
            "invocation_ref": None,
            "tool_name": "plan.patch",
            "primary_entity_type": "plan",
            "primary_entity_ref": target,
            "forward_patch": {"changes": {"weekly_minutes": after}},
            "inverse_patch": {"changes": {"weekly_minutes": before}},
            "status": "committed",
            "created_at": f"2026-08-31T00:00:0{ordinal}.000000Z",
            "undone_at": None,
        },
        ordinal + 1,
    )


def test_four_existing_runtime_minis_export_only_v2_and_validate(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, summary = e2_batch
    episodes = [_load(path) for path in sorted((output / "episodes").glob("*.json"))]
    assert len(episodes) == 4
    assert set(summary.tracks) == {"planning", "intervention", "assessment", "revision"}
    assert {episode["schema_version"] for episode in episodes} == {
        "decision-episode-v2"
    }
    assert all(not validate_episode(episode) for episode in episodes)
    assert all(DecisionEpisodeV2.model_validate(episode) for episode in episodes)
    assert not any(b"lossy_v1_projection" in data for data in _files(output).values())
    manifest = _load(output / "run-manifest.json")
    assert manifest["schema_version"] == "e2-run-output-manifest-v1"
    assert manifest["episode_schema_version"] == "decision-episode-v2"
    assert manifest["formal_evaluation_result"] is False
    planning = next(episode for episode in episodes if episode["track"] == "planning")
    planning_guard = planning["observable_trace"]["guard_decisions"][0]
    assert planning_guard["reason_code"] == "plan_adoption_required"
    assert planning_guard["decision_ref"].startswith("plan_proposal:")
    intervention = next(
        episode for episode in episodes if episode["track"] == "intervention"
    )
    guard = intervention["observable_trace"]["guard_decisions"][0]
    assert guard["decision_ref"].startswith("proactive_decision:")


def test_exporter_classification_has_no_track_fixture_or_oracle_branch() -> None:
    source = inspect.getsource(_effects_and_action)
    assert "track" not in source
    assert "seed_kind" not in source
    assert "oracle" not in source
    assert "P-E1" not in source
    assert not (
        PROJECT_ROOT / "evaluation/src/learning_agent_eval/runtime_export.py"
    ).exists()


def test_worker_loads_oracle_only_after_runtime_and_actual_delta() -> None:
    source = inspect.getsource(_execute)
    oracle_load = source.index('_load_json(Path(request["oracle_path"]))')
    assert source.index("await runtime.run") < oracle_load
    assert source.index("build_state_delta") < oracle_load


def test_snapshots_and_delta_are_recomputable_and_checkpoint_independent(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    for path in sorted((output / "episodes").glob("*.json")):
        episode = _load(path)
        assert episode["state_before"]["snapshot_sha256"] == state_snapshot_digest(
            episode["state_before"]
        )
        assert episode["state_after"]["snapshot_sha256"] == state_snapshot_digest(
            episode["state_after"]
        )
        assert episode["state_before"]["captured_at"].endswith("Z")
        assert episode["state_after"]["captured_at"].endswith("Z")
        assert episode["environment"]["frozen_time"].endswith("Z")
        assert episode["trigger"]["triggered_at"].endswith("Z")
        assert episode["provenance"]["created_at"].endswith("Z")
        assert {
            episode["state_before"]["captured_at"],
            episode["state_after"]["captured_at"],
            episode["environment"]["frozen_time"],
            episode["trigger"]["triggered_at"],
            episode["provenance"]["created_at"],
        } == {episode["environment"]["frozen_time"]}
        assert (
            build_state_delta(episode["state_before"], episode["state_after"])
            == episode["state_delta"]
        )
        run_entities = [
            item
            for item in episode["state_after"]["logical_entities"]
            if item["entity_type"] == "agent_run"
        ]
        assert len(run_entities) == 1
        assert "checkpoint" not in run_entities[0]["data"]
        assert run_entities[0]["data"]["status"] == "completed"


def test_operation_alignment_covers_multi_entity_and_operation_free_effects(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    assessment = _load(output / "episodes/A-E1-MINI-001.json")
    affected_types = {
        next(
            entity["entity_type"]
            for entity in assessment["state_after"]["logical_entities"]
            if entity["logical_id"] == reference
        )
        for reference in assessment["observable_trace"]["operations"][0][
            "affected_entity_refs"
        ]
    }
    assert affected_types == {"plan", "stage", "submission", "task"}
    assert all(
        change["operation_alignment"] == "matched"
        for change in assessment["state_delta"]["changes"]
        if change["entity_ref"]
        in assessment["observable_trace"]["operations"][0]["affected_entity_refs"]
    )
    for entity_type in ("artifact", "evidence_observation", "learning_event"):
        audit_change = next(
            change
            for change in assessment["state_delta"]["changes"]
            if change["entity_ref"].startswith(f"{entity_type}:")
        )
        assert audit_change["operation_refs"] == []
        assert audit_change["operation_alignment"] == "not_applicable"
        assert any(
            source["source_type"] in {"runtime", "entity"}
            for source in audit_change["source_refs"]
        )
    planning = _load(output / "episodes/P-E1-MINI-001.json")
    proposal_change = next(
        change
        for change in planning["state_delta"]["changes"]
        if change["entity_ref"].startswith("plan_proposal:")
    )
    assert proposal_change["operation_refs"] == []
    assert proposal_change["operation_alignment"] == "not_applicable"
    assert _execution_status("needs_reconciliation") == "completed"
    assert _execution_status("retry_pending") == "completed"


def test_normalizer_is_deterministic_and_fails_closed() -> None:
    assert normalize_rfc3339("2026-08-31T09:00:00+08:00") == (
        "2026-08-31T01:00:00.000000Z"
    )
    with pytest.raises(NormalizationError, match="naive_timestamp"):
        normalize_rfc3339("2026-08-31T09:00:00")
    first = normalize_json(
        {
            "set": {"b", "a"},
            "list": ["b", "a"],
            "nested": {"z": -0.0, "a": 2.0},
        }
    )
    second = normalize_json(
        {"nested": {"a": 2, "z": 0}, "list": ["b", "a"], "set": {"a", "b"}}
    )
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["list"] == ["b", "a"]
    assert first["set"] == ["a", "b"]
    with pytest.raises(NormalizationError, match="unsupported_type"):
        normalize_json(object())
    with pytest.raises(NormalizationError, match="non_finite"):
        normalize_json(float("inf"))
    with pytest.raises(NormalizationError, match="prohibited_field"):
        normalize_json({"claim_token": "synthetic"})
    registry = StableIdentityRegistry(episode_id="E2-TIME-001")
    assert normalize_reference_fields(
        {
            "created_at": "2026-08-31T09:00:00+08:00",
            "deadline": "2026-09-01T09:00:00+08:00",
        },
        registry,
    ) == {
        "created_at": "2026-08-31T01:00:00.000000Z",
        "deadline": "2026-09-01T01:00:00.000000Z",
    }
    phone_shaped_digest = (
        "13133501648d78dbb0b0eb456d59e6381af7575de8f3b4c2b767b9a0346b84cc"
    )
    assert not privacy_issues({"result_sha256": phone_shaped_digest})
    assert privacy_issues({"result_sha256": "13133501648"})


def test_stable_identity_ignores_raw_ids_and_insertion_order() -> None:
    def mapped(rows: list[IdentityCandidate]) -> list[str | None]:
        registry = StableIdentityRegistry(episode_id="E2-ID-001")
        registry.register_many("task", rows)
        return [registry.resolve("task", raw_id) for raw_id in ("random-a", "random-b")]

    first = mapped(
        [
            IdentityCandidate("random-b", {"position": 2, "title": "B"}),
            IdentityCandidate("random-a", {"position": 1, "title": "A"}),
        ]
    )
    second = mapped(
        [
            IdentityCandidate("random-a", {"position": 1, "title": "A"}),
            IdentityCandidate("random-b", {"position": 2, "title": "B"}),
        ]
    )
    assert first == second == ["task:E2-ID-001:001", "task:E2-ID-001:002"]
    with pytest.raises(NormalizationError, match="ambiguous_semantic_key"):
        registry = StableIdentityRegistry(episode_id="E2-ID-002")
        registry.register_many(
            "task",
            [
                IdentityCandidate(1, {"title": "same"}),
                IdentityCandidate(2, {"title": "same"}),
            ],
        )


def test_delta_distinguishes_create_delete_null_missing_nested_and_stable_list() -> (
    None
):
    before = _snapshot(
        [
            _entity(
                "constraint:test:001",
                "constraint",
                {
                    "scalar": 1,
                    "nullable": "present",
                    "nested": {"value": 1},
                    "items": [{"key": "a", "value": 1}, {"key": "remove", "value": 2}],
                },
                1,
            ),
            _entity("goal:test:removed", "goal", {"value": 1}, 2),
        ]
    )
    after = _snapshot(
        [
            _entity(
                "constraint:test:001",
                "constraint",
                {
                    "scalar": 2,
                    "nullable": None,
                    "nested": {"value": 2, "added": True},
                    "items": [{"key": "a", "value": 3}, {"key": "new", "value": 4}],
                },
                1,
            ),
            _entity("goal:test:added", "goal", {"value": 2}, 2),
        ]
    )
    delta = build_state_delta(before, after)
    assert delta["capture_status"] == "complete"
    assert {change["kind"] for change in delta["changes"]} == {
        "added",
        "removed",
        "changed",
    }
    nullable = next(
        change for change in delta["changes"] if change["field_path"] == "data.nullable"
    )
    assert nullable["after"] == {"presence": "present", "value": None}
    added = next(
        change
        for change in delta["changes"]
        if change["field_path"] == "data.nested.added"
    )
    assert added["before"] == {"presence": "missing", "value": None}
    assert any(
        change["field_path"] == "data.items[0].value" for change in delta["changes"]
    )
    assert any(
        change["field_path"] == "$entity" and change["kind"] == "removed"
        for change in delta["changes"]
    )


def test_operation_backed_entity_create_and_delete_require_explicit_patch_markers() -> (
    None
):
    target = "plan:test:001"
    anchor = _entity("constraint:test:001", "constraint", {"value": 1}, 1)
    operation = _entity(
        "operation:test:001",
        "operation",
        {
            "run_ref": "agent_run:test:001",
            "invocation_ref": None,
            "tool_name": "plan.create",
            "primary_entity_type": "plan",
            "primary_entity_ref": target,
            "forward_patch": {"created_ref": target},
            "inverse_patch": {"delete_ref": target},
            "status": "committed",
            "created_at": "2026-08-31T00:00:00.000000Z",
            "undone_at": None,
        },
        3,
    )
    created = build_state_delta(
        _snapshot([anchor]),
        _snapshot([anchor, _entity(target, "plan", {"status": "draft"}, 2), operation]),
    )
    create_change = next(
        item for item in created["changes"] if item["entity_ref"] == target
    )
    assert create_change["operation_alignment"] == "matched"
    assert create_change["operation_refs"] == ["operation:test:001"]

    delete_operation = json.loads(json.dumps(operation))
    delete_operation["data"]["forward_patch"] = {"delete_ref": target}
    delete_operation["data"]["inverse_patch"] = {"created_ref": target}
    deleted = build_state_delta(
        _snapshot([anchor, _entity(target, "plan", {"status": "draft"}, 2)]),
        _snapshot([anchor, delete_operation]),
    )
    delete_change = next(
        item for item in deleted["changes"] if item["entity_ref"] == target
    )
    assert delete_change["operation_alignment"] == "matched"
    assert delete_change["operation_refs"] == ["operation:test:001"]

    mismatched_operation = json.loads(json.dumps(operation))
    mismatched_operation["data"]["forward_patch"] = {"created_ref": "plan:test:002"}
    invalid = build_state_delta(
        _snapshot([anchor]),
        _snapshot(
            [
                anchor,
                _entity(target, "plan", {"status": "draft"}, 2),
                mismatched_operation,
            ]
        ),
    )
    assert invalid["capture_status"] == "incomplete"
    assert invalid["error_codes"] == ["delta.operation_mismatch.plan"]


def test_empty_delta_is_confirmed_by_compared_and_unchanged_refs() -> None:
    entity = _entity("constraint:test:001", "constraint", {"value": 1}, 1)
    before = _snapshot([entity])
    after = _snapshot([dict(entity)])
    delta = build_state_delta(before, after)
    assert delta["changes"] == []
    assert delta["compared_entity_refs"] == ["constraint:test:001"]
    assert delta["unchanged_entity_refs"] == ["constraint:test:001"]
    assert delta["capture_status"] == "complete"


def test_multi_operation_chain_and_mismatch_are_explicit() -> None:
    target = "plan:test:001"
    before = _snapshot([_entity(target, "plan", {"weekly_minutes": 100}, 1)])
    after = _snapshot(
        [
            _entity(target, "plan", {"weekly_minutes": 300}, 1),
            _operation(1, target=target, before=100, after=200),
            _operation(2, target=target, before=200, after=300),
        ]
    )
    delta = build_state_delta(before, after)
    change = next(
        change for change in delta["changes"] if change["entity_ref"] == target
    )
    assert change["operation_alignment"] == "matched"
    assert change["operation_refs"] == ["operation:test:001", "operation:test:002"]
    mismatched = _snapshot(
        [
            _entity(target, "plan", {"weekly_minutes": 350}, 1),
            _operation(1, target=target, before=100, after=200),
        ]
    )
    invalid = build_state_delta(before, mismatched)
    assert invalid["capture_status"] == "incomplete"
    assert invalid["error_codes"] == ["delta.operation_mismatch.plan"]


def test_wait_no_op_blocked_and_deferred_are_structurally_classified() -> None:
    def classify(
        outcome: str | None, tool_status: str | None = None
    ) -> tuple[str, str, str]:
        entities = []
        if outcome is not None:
            entities.append(
                _entity(
                    "proactive_decision:test:001",
                    "proactive_decision",
                    {
                        "outcome": outcome,
                        "policy_version": "policy:test:v1",
                        "invocation_ref": (
                            "tool_invocation:test:001"
                            if tool_status is not None
                            else None
                        ),
                    },
                    1,
                )
            )
        if tool_status == "pending_approval":
            entities.append(
                _entity(
                    "run_approval:test:001",
                    "run_approval",
                    {
                        "invocation_ref": "tool_invocation:test:001",
                        "decision": "pending",
                    },
                    len(entities) + 1,
                )
            )
        after = {"logical_entities": entities}
        trace = {
            "tool_invocations": (
                [
                    {
                        "invocation_id": "tool_invocation:test:001",
                        "tool_name": "notification.send",
                        "durable_status": tool_status,
                        "execution_status": "blocked"
                        if tool_status == "rejected"
                        else "completed",
                    }
                ]
                if tool_status is not None
                else []
            ),
            "operations": [],
        }
        effects, action, guard, _ = _effects_and_action(
            episode_id="E2-BOUNDARY-001",
            after=after,
            delta={"changes": []},
            trace=trace,
        )
        return effects[0]["effect_type"], action, guard

    assert classify("success_wait") == ("wait", "WAIT", "allowed")
    assert classify(None) == ("no_op", "NO_OP", "not_evaluated")
    assert classify("guard_rejected", "rejected") == (
        "blocked",
        "INTERVENE_MESSAGE",
        "blocked",
    )
    assert classify("deferred_quiet_hours", "committed") == (
        "deferred",
        "INTERVENE_MESSAGE",
        "deferred",
    )
    assert classify(None, "pending_approval") == (
        "approval_request",
        "REQUEST_APPROVAL",
        "deferred",
    )


def test_trace_preserves_multiple_model_turns_and_tool_calls() -> None:
    after = {
        "logical_entities": [
            _entity(
                f"tool_invocation:test:{ordinal:03d}",
                "tool_invocation",
                {
                    "created_at": f"2026-08-31T00:00:0{ordinal}.000000Z",
                    "tool_call_id": f"call:test:{ordinal}",
                    "tool_name": "synthetic.read",
                    "canonical_args": {"ordinal": ordinal},
                    "status": "committed",
                    "result_payload": {"ordinal": ordinal},
                },
                ordinal,
            )
            for ordinal in (1, 2)
        ]
    }
    records = [
        {
            "ordinal": ordinal,
            "visible_input_digest": str(ordinal) * 64,
            "assistant_text": "",
            "function_calls": [{"call_id": f"call:test:{ordinal}"}],
        }
        for ordinal in (1, 2)
    ]
    trace, attempts = _trace(
        episode_id="E2-MULTI-001", model_records=records, after=after
    )
    assert [item["ordinal"] for item in trace["model_turns"]] == [1, 2]
    assert [item["ordinal"] for item in trace["tool_invocations"]] == [1, 2]
    assert [item["invocation_refs"] for item in attempts] == [
        ["tool_invocation:test:001"],
        ["tool_invocation:test:002"],
    ]


@pytest.mark.parametrize(
    ("episode_id", "pack"),
    [
        ("I-E1-MINI-001", "intervention"),
        ("A-E1-MINI-001", "assessment"),
        ("R-E1-MINI-001", "revision"),
    ],
)
def test_track_rules_select_structured_tool_among_multiple_invocations(
    e2_batch: tuple[Path, RunAgentSummary], episode_id: str, pack: str
) -> None:
    output, _ = e2_batch
    episode = _load(output / "episodes" / f"{episode_id}.json")
    invocations = episode["observable_trace"]["tool_invocations"]
    for ordinal, invocation in enumerate(invocations, 2):
        invocation["ordinal"] = ordinal
    invocations.insert(
        0,
        {
            "invocation_id": f"tool_invocation:{episode_id}:read-only",
            "ordinal": 1,
            "tool_call_id": f"call:{episode_id}:read-only",
            "tool_name": "profile.get",
            "canonical_args": {},
            "execution_status": "completed",
            "observation_status": "succeeded",
            "durable_status": "committed",
            "result": {"status": "ok"},
            "result_digest": sha256_digest({"status": "ok"}),
            "operation_refs": [],
        },
    )
    checks = [
        check
        for check in evaluate_rules(episode)["checks"]
        if check["rule_pack"] == pack
    ]
    assert {check["status"] for check in checks} == {"pass"}


def test_runtime_guard_deferred_preserves_attempt_and_zero_side_effects(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    source = dataset / "fixtures/mini/I-E1-MINI-001.json"
    fixture = _load(source)
    fixture.update(
        {
            "episode_id": "I-E2-DEFERRED-001",
            "scenario_family_id": "I-E2-DEFERRED-FAMILY-001",
            "frozen_time": "2026-08-31T15:30:00.000000Z",
            "owner_id": "owner:e2:deferred:001",
            "run_id": "run:e2:deferred:001",
            "session_id": "session:e2:deferred:001",
        }
    )
    fixture["trigger"]["trigger_id"] = "trigger:e2:deferred:001"
    fixture["trigger"]["triggered_at"] = fixture["frozen_time"]
    oracle = _load(dataset / "oracles/mini/I-E1-MINI-001.json")
    oracle["must_satisfy"][0]["id"] = "oracle:e2:i:deferred"
    oracle["must_satisfy"][0]["statement"] = (
        "The production Guard must defer delivery without durable notification effects."
    )
    oracle["expected_effects"][0]["value"] = "deferred"
    oracle["envelope_sha256"] = sha256_digest(
        {key: value for key, value in oracle.items() if key != "envelope_sha256"}
    )
    oracle_path = dataset / "oracles/mini/I-E2-DEFERRED-001.json"
    oracle_path.write_bytes(canonical_json_bytes(oracle))
    fixture["oracle_file"] = "oracles/mini/I-E2-DEFERRED-001.json"
    fixture["fixture_sha256"] = sha256_digest(
        {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    )
    target = dataset / "fixtures/mini/I-E2-DEFERRED-001.json"
    target.write_bytes(canonical_json_bytes(fixture))
    manifest = _load(dataset / "manifests/e1-mini-stub.json")
    manifest["fixture_files"] = ["fixtures/mini/I-E2-DEFERRED-001.json"]
    manifest["manifest_sha256"] = sha256_digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    manifest_path = dataset / "manifests/e2-deferred-stub.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    output = tmp_path / "output"
    run_agent(dataset=dataset, manifest=manifest_path, output=output)
    episode = _load(output / "episodes/I-E2-DEFERRED-001.json")
    assert episode["result"]["guard"]["status"] == "deferred"
    assert (
        episode["observable_trace"]["tool_invocations"][0]["observation_status"]
        == "deferred"
    )
    assert episode["result"]["action_class"] == "INTERVENE_MESSAGE"
    assert episode["result"]["layers"]["model_attempts"][0]["invocation_refs"]
    assert episode["result"]["layers"]["final_effects"] == [
        {
            **episode["result"]["layers"]["final_effects"][0],
            "effect_type": "deferred",
            "status": "deferred",
            "entity_refs": [],
        }
    ]
    assert not {
        "intervention",
        "notification",
        "outbox_action",
        "outbox_receipt",
    } & {item["entity_type"] for item in episode["state_after"]["logical_entities"]}
    assert episode["isolation_evidence"]["recording_sink_attempts"] == 0
    assert episode["isolation_evidence"]["outbox_replay_confirmed"] is False
    assert not validate_episode(episode)
    assert evaluate_rules(episode)["status"] == "pass"


def test_runtime_assessment_accept_exports_complete_operation_attribution(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    fixture = _load(dataset / "fixtures/mini/A-E1-MINI-001.json")
    fixture.update(
        {
            "episode_id": "A-E2-ACCEPT-001",
            "scenario_family_id": "A-E2-ACCEPT-FAMILY-001",
            "owner_id": "owner:e2:accept:001",
            "run_id": "run:e2:accept:001",
            "session_id": "session:e2:accept:001",
        }
    )
    fixture["trigger"]["trigger_id"] = "trigger:e2:accept:001"
    arguments = fixture["scripted_turns"][0]["tool_calls"][0]["arguments"]
    arguments["score"] = 85
    arguments["checks"] = [{"criterion": "measurement_evidence", "passed": True}]
    arguments["feedback"] = "The synthetic measurement evidence meets the threshold."
    fixture["scripted_turns"][1]["assistant_text"] = (
        "The synthetic submission meets the declared threshold."
    )
    oracle = _load(dataset / "oracles/mini/A-E1-MINI-001.json")
    oracle["allowed_action_classes"] = ["ACCEPT"]
    oracle["must_satisfy"][0]["id"] = "oracle:e2:a:accepted-verdict"
    oracle["must_satisfy"][0]["statement"] = (
        "The production assessment tool must persist the accepted verdict and award state."
    )
    oracle["expected_effects"][0]["value"] = "ACCEPT"
    oracle["envelope_sha256"] = sha256_digest(
        {key: value for key, value in oracle.items() if key != "envelope_sha256"}
    )
    oracle_path = dataset / "oracles/mini/A-E2-ACCEPT-001.json"
    oracle_path.write_bytes(canonical_json_bytes(oracle))
    fixture["oracle_file"] = "oracles/mini/A-E2-ACCEPT-001.json"
    fixture["fixture_sha256"] = sha256_digest(
        {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    )
    fixture_path = dataset / "fixtures/mini/A-E2-ACCEPT-001.json"
    fixture_path.write_bytes(canonical_json_bytes(fixture))
    manifest = _load(dataset / "manifests/e1-mini-stub.json")
    manifest["fixture_files"] = ["fixtures/mini/A-E2-ACCEPT-001.json"]
    manifest["manifest_sha256"] = sha256_digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    manifest_path = dataset / "manifests/e2-accept-stub.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    output = tmp_path / "output"
    run_agent(dataset=dataset, manifest=manifest_path, output=output)
    episode = _load(output / "episodes/A-E2-ACCEPT-001.json")
    assert not validate_episode(episode)
    assert episode["result"]["action_class"] == "ACCEPT"
    assert episode["state_delta"]["capture_status"] == "complete"
    entity_types = {
        item["logical_id"]: item["entity_type"]
        for item in episode["state_after"]["logical_entities"]
    }
    changed_types = {
        entity_types[item["entity_ref"]]
        for item in episode["state_delta"]["changes"]
        if item["kind"] == "changed"
    }
    assert {"plan", "stage", "task", "submission"}.issubset(changed_types)
    assert all(
        item["operation_alignment"] == "matched"
        for item in episode["state_delta"]["changes"]
        if entity_types[item["entity_ref"]] in {"plan", "stage", "task", "submission"}
        and item["field_path"]
        not in {"data.created_at", "data.updated_at", "data.version"}
    )
    assert evaluate_rules(episode)["status"] == "pass"


def test_runtime_pending_approval_is_a_complete_deferred_episode(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    fixture = _load(dataset / "fixtures/mini/R-E1-MINI-001.json")
    fixture.update(
        {
            "episode_id": "R-E2-APPROVAL-001",
            "scenario_family_id": "R-E2-APPROVAL-FAMILY-001",
            "owner_id": "owner:e2:approval:001",
            "run_id": "run:e2:approval:001",
            "session_id": "session:e2:approval:001",
        }
    )
    fixture["trigger"]["trigger_id"] = "trigger:e2:approval:001"
    arguments = fixture["scripted_turns"][0]["tool_calls"][0]["arguments"]
    arguments["goal"] = "Apply a synthetic protected goal change after approval."
    fixture["scripted_turns"] = fixture["scripted_turns"][:1]
    oracle = _load(dataset / "oracles/mini/R-E1-MINI-001.json")
    oracle["allowed_action_classes"] = ["REQUEST_APPROVAL"]
    oracle["must_satisfy"] = [
        {
            "id": "oracle:e2:r:approval-boundary",
            "statement": "The protected background change must pause for durable approval.",
            "evidence_paths": [
                "result.action_class",
                "observable_trace.guard_decisions[0].decision_ref",
                "state_after.logical_entities",
            ],
        }
    ]
    oracle["expected_effects"] = [
        {
            "path": "result.action_class",
            "relation": "equals",
            "value": "REQUEST_APPROVAL",
        }
    ]
    oracle["envelope_sha256"] = sha256_digest(
        {key: value for key, value in oracle.items() if key != "envelope_sha256"}
    )
    oracle_path = dataset / "oracles/mini/R-E2-APPROVAL-001.json"
    oracle_path.write_bytes(canonical_json_bytes(oracle))
    fixture["oracle_file"] = "oracles/mini/R-E2-APPROVAL-001.json"
    fixture["fixture_sha256"] = sha256_digest(
        {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    )
    fixture_path = dataset / "fixtures/mini/R-E2-APPROVAL-001.json"
    fixture_path.write_bytes(canonical_json_bytes(fixture))
    manifest = _load(dataset / "manifests/e1-mini-stub.json")
    manifest["fixture_files"] = ["fixtures/mini/R-E2-APPROVAL-001.json"]
    manifest["manifest_sha256"] = sha256_digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    manifest_path = dataset / "manifests/e2-approval-stub.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    output = tmp_path / "output"
    run_agent(dataset=dataset, manifest=manifest_path, output=output)
    episode = _load(output / "episodes/R-E2-APPROVAL-001.json")
    invocation = episode["observable_trace"]["tool_invocations"][0]
    approvals = [
        item
        for item in episode["state_after"]["logical_entities"]
        if item["entity_type"] == "run_approval"
    ]
    assert episode["result"]["action_class"] == "REQUEST_APPROVAL"
    assert episode["result"]["guard"] == {
        "status": "deferred",
        "reason_code": "approval_required",
        "blocked_effect_refs": [],
    }
    assert episode["result"]["layers"]["run_status"] == "waiting_approval"
    assert episode["result"]["layers"]["durable_status"] == "deferred"
    assert invocation["execution_status"] == "not_executed"
    assert invocation["observation_status"] == "pending_approval"
    assert invocation["durable_status"] == "pending_approval"
    assert episode["observable_trace"]["operations"] == []
    assert len(approvals) == 1
    assert approvals[0]["data"]["decision"] == "pending"
    assert (
        approvals[0]["logical_id"]
        in episode["result"]["layers"]["final_effects"][0]["entity_refs"]
    )
    encoded = canonical_json_bytes(episode).decode("utf-8")
    assert '"approval_id"' not in encoded
    assert '"plan_id"' not in encoded
    assert not validate_episode(episode)
    result = evaluate_rules(episode)
    assert result["status"] == "pass"
    revision_checks = {
        item["check_id"]: item["status"]
        for item in result["checks"]
        if item["rule_pack"] == "revision"
    }
    assert revision_checks["revision.approval_boundary"] == "pass"
    assert revision_checks["revision.reversible_patch"] == "not_applicable"
    assert revision_checks["revision.operation_delta_alignment"] == "not_applicable"
    assert revision_checks["revision.durable_success"] == "not_applicable"


@pytest.mark.parametrize(
    ("fixture_name", "prefix", "expected_action", "expected_effect"),
    [
        ("I-E1-MINI-001.json", "wait", "WAIT", "wait"),
        ("R-E1-MINI-001.json", "no-op", "NO_OP", "no_op"),
    ],
)
def test_runtime_no_tool_wait_and_no_op_are_complete_episodes(
    tmp_path: Path,
    fixture_name: str,
    prefix: str,
    expected_action: str,
    expected_effect: str,
) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    fixture = _load(dataset / "fixtures/mini" / fixture_name)
    episode_id = f"{fixture['track'][0].upper()}-E2-{prefix.upper()}-001"
    fixture.update(
        {
            "episode_id": episode_id,
            "scenario_family_id": f"{episode_id}-FAMILY",
            "owner_id": f"owner:e2:{prefix}:001",
            "run_id": f"run:e2:{prefix}:001",
        }
    )
    if fixture["session_id"] is not None:
        fixture["session_id"] = f"session:e2:{prefix}:001"
    fixture["trigger"]["trigger_id"] = f"trigger:e2:{prefix}:001"
    fixture["scripted_turns"] = [
        {
            "ordinal": 1,
            "delivery": "stream",
            "assistant_text": "No durable learning-state change is required.",
            "tool_calls": [],
        }
    ]
    oracle = _load(dataset / "oracles/mini" / fixture["oracle_file"].split("/")[-1])
    oracle["allowed_action_classes"] = [expected_action]
    oracle["must_satisfy"] = [
        {
            "id": f"oracle:e2:{prefix}:no-tool",
            "statement": "The production Runtime must preserve a complete no-tool decision.",
            "evidence_paths": [
                "result.action_class",
                "observable_trace.model_turns",
                "state_delta.changes",
            ],
        }
    ]
    oracle["expected_effects"] = [
        {
            "path": "result.action_class",
            "relation": "equals",
            "value": expected_action,
        }
    ]
    oracle["envelope_sha256"] = sha256_digest(
        {key: value for key, value in oracle.items() if key != "envelope_sha256"}
    )
    oracle_name = f"{episode_id}.json"
    (dataset / "oracles/mini" / oracle_name).write_bytes(canonical_json_bytes(oracle))
    fixture["oracle_file"] = f"oracles/mini/{oracle_name}"
    fixture["fixture_sha256"] = sha256_digest(
        {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    )
    fixture_path = dataset / "fixtures/mini" / oracle_name
    fixture_path.write_bytes(canonical_json_bytes(fixture))
    manifest = _load(dataset / "manifests/e1-mini-stub.json")
    manifest["fixture_files"] = [f"fixtures/mini/{oracle_name}"]
    manifest["manifest_sha256"] = sha256_digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    manifest_path = dataset / "manifests" / f"e2-{prefix}-stub.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    output = tmp_path / "output"
    run_agent(dataset=dataset, manifest=manifest_path, output=output)
    episode = _load(output / "episodes" / oracle_name)
    assert episode["result"]["action_class"] == expected_action
    assert (
        episode["result"]["layers"]["final_effects"][0]["effect_type"]
        == expected_effect
    )
    assert episode["result"]["layers"]["final_effects"][0]["status"] == "no_change"
    assert (
        episode["result"]["layers"]["model_attempts"][-1]["attempted_action"]
        == expected_effect
    )
    assert episode["observable_trace"]["tool_invocations"] == []
    assert episode["observable_trace"]["operations"] == []
    assert not validate_episode(episode)
    assert evaluate_rules(episode)["status"] == "pass"


def test_runtime_guard_blocked_preserves_attempt_and_zero_side_effects(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    fixture = _load(dataset / "fixtures/mini/I-E1-MINI-001.json")
    fixture.update(
        {
            "episode_id": "I-E2-BLOCKED-001",
            "scenario_family_id": "I-E2-BLOCKED-FAMILY-001",
            "owner_id": "owner:e2:blocked:001",
            "run_id": "run:e2:blocked:001",
            "session_id": "session:e2:blocked:001",
        }
    )
    fixture["seed"]["daily_notification_limit"] = 0
    fixture["trigger"]["trigger_id"] = "trigger:e2:blocked:001"
    oracle = _load(dataset / "oracles/mini/I-E1-MINI-001.json")
    oracle["must_satisfy"][0]["id"] = "oracle:e2:i:blocked"
    oracle["must_satisfy"][0]["statement"] = (
        "The production Guard must block delivery without durable notification effects."
    )
    oracle["expected_effects"][0]["value"] = "blocked"
    oracle["envelope_sha256"] = sha256_digest(
        {key: value for key, value in oracle.items() if key != "envelope_sha256"}
    )
    oracle_path = dataset / "oracles/mini/I-E2-BLOCKED-001.json"
    oracle_path.write_bytes(canonical_json_bytes(oracle))
    fixture["oracle_file"] = "oracles/mini/I-E2-BLOCKED-001.json"
    fixture["fixture_sha256"] = sha256_digest(
        {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    )
    target = dataset / "fixtures/mini/I-E2-BLOCKED-001.json"
    target.write_bytes(canonical_json_bytes(fixture))
    manifest = _load(dataset / "manifests/e1-mini-stub.json")
    manifest["fixture_files"] = ["fixtures/mini/I-E2-BLOCKED-001.json"]
    manifest["manifest_sha256"] = sha256_digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    manifest_path = dataset / "manifests/e2-blocked-stub.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    output = tmp_path / "output"
    run_agent(dataset=dataset, manifest=manifest_path, output=output)
    episode = _load(output / "episodes/I-E2-BLOCKED-001.json")
    assert episode["result"]["guard"]["status"] == "blocked"
    assert (
        episode["observable_trace"]["tool_invocations"][0]["observation_status"]
        == "blocked"
    )
    assert episode["result"]["action_class"] == "INTERVENE_MESSAGE"
    assert episode["result"]["layers"]["model_attempts"][0]["invocation_refs"]
    assert episode["result"]["layers"]["final_effects"][0]["effect_type"] == "blocked"
    assert not {
        "intervention",
        "notification",
        "outbox_action",
        "outbox_receipt",
    } & {item["entity_type"] for item in episode["state_after"]["logical_entities"]}
    assert episode["isolation_evidence"]["recording_sink_attempts"] == 0
    assert not validate_episode(episode)
    assert evaluate_rules(episode)["status"] == "pass"


def test_no_database_identity_or_routing_material_is_exported(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    encoded = b"\n".join(_files(output).values()).decode("utf-8")
    prohibited_keys = (
        '"owner_id"',
        '"plan_id"',
        '"task_id"',
        '"submission_id"',
        '"run_id"',
        '"source_id"',
        '"source_uri"',
        '"snapshot_id"',
        '"canonical_message_id"',
        '"reply_to_intervention_id"',
        '"claim_token"',
        '"reply_token"',
        '"endpoint"',
        '"keys"',
    )
    assert all(key not in encoded for key in prohibited_keys)
    assert "submission:1" not in encoded
    assert "plan:1" not in encoded
    assert "recipient@example.test" not in encoded
    for path in sorted((output / "captures").glob("*.json")):
        assert all(
            record.get("visible_messages_omitted") is True
            and "visible_messages" not in record
            for record in _load(path)["model_records"]
        )


def test_rule_results_are_strict_recomputable_and_use_episode_evidence(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    for path in sorted((output / "episodes").glob("*.json")):
        episode = _load(path)
        result = evaluate_rules(episode)
        RuleResultV1.model_validate(result)
        assert result["status"] == "pass"
        assert result["hard_gates"] == []
        assert len(result["checks"]) == 52
        assert result["formal_evaluation_result"] is False
        assert {check["severity"] for check in result["checks"]} == {
            "minor",
            "major",
            "critical",
        }
        for check in result["checks"]:
            assert all(
                not evidence.startswith("capture.")
                for evidence in check["evidence_paths"]
            )
            assert all(
                resolve_evidence_path(episode, evidence)[0]
                for evidence in check["evidence_paths"]
            )


def test_every_rule_has_pass_and_missing_input_coverage(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    source_by_pack = {
        "common": "P-E1-MINI-001",
        "planning": "P-E1-MINI-001",
        "intervention": "I-E1-MINI-001",
        "assessment": "A-E1-MINI-001",
        "revision": "R-E1-MINI-001",
        "trace": "P-E1-MINI-001",
        "isolation": "P-E1-MINI-001",
    }
    episodes = {
        episode_id: _load(output / "episodes" / f"{episode_id}.json")
        for episode_id in set(source_by_pack.values())
    }
    checks = {
        check["check_id"]: check
        for pack, episode_id in source_by_pack.items()
        for check in evaluate_rules(episodes[episode_id])["checks"]
        if check["rule_pack"] == pack
    }
    assert len(checks) == 52
    assert {check["status"] for check in checks.values()} == {"pass"}
    for check_id, baseline in checks.items():
        assert baseline["evidence_paths"], check_id
        episode_id = source_by_pack[baseline["rule_pack"]]
        missing = json.loads(json.dumps(episodes[episode_id]))
        _delete_evidence_path(missing, baseline["evidence_paths"][0])
        target = next(
            item
            for item in evaluate_rules(missing)["checks"]
            if item["check_id"] == check_id
        )
        assert target["status"] == "invalid_input", check_id


def test_every_rule_has_a_structured_fail_boundary(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    source_by_pack = {
        "common": "P-E1-MINI-001",
        "planning": "P-E1-MINI-001",
        "intervention": "I-E1-MINI-001",
        "assessment": "A-E1-MINI-001",
        "revision": "R-E1-MINI-001",
        "trace": "P-E1-MINI-001",
        "isolation": "P-E1-MINI-001",
    }
    episodes = {
        episode_id: _load(output / "episodes" / f"{episode_id}.json")
        for episode_id in set(source_by_pack.values())
    }

    def entity(document: dict[str, Any], kind: str) -> dict[str, Any]:
        return next(
            item
            for item in document["state_after"]["logical_entities"]
            if item["entity_type"] == kind
        )

    def violate(check_id: str, document: dict[str, Any]) -> None:
        invocation = document["observable_trace"]["tool_invocations"]
        if check_id == "common.schema":
            document["result"]["action_mapping_sha256"] = "0" * 64
        elif check_id == "common.episode_digest":
            document["provenance"]["episode_sha256"] = "0" * 64
        elif check_id == "common.completeness":
            document["completeness"]["status"] = "invalid"
        elif check_id == "common.runtime_provenance":
            document["provenance"]["runtime_executed"] = False
        elif check_id == "common.stub_nonformal":
            document["provenance"]["formal_evaluation_result"] = True
        elif check_id == "common.oracle_review":
            document["oracle"]["oracle_reviewer"] = document["oracle"]["oracle_author"]
        elif check_id == "common.environment_digest":
            document["environment"]["manifest_sha256"] = "0" * 64
        elif check_id == "common.action_envelope":
            document["oracle"]["allowed_action_classes"] = ["WAIT"]
        elif check_id == "planning.proposal_present":
            document["state_after"]["logical_entities"] = [
                item
                for item in document["state_after"]["logical_entities"]
                if item["entity_type"] != "plan_proposal"
            ]
        elif check_id == "planning.pending_boundary":
            entity(document, "plan_proposal")["data"]["status"] = "accepted"
        elif check_id == "planning.weekly_budget":
            entity(document, "plan_proposal")["data"]["plan_payload"][
                "weekly_minutes"
            ] = 0
        elif check_id == "planning.deadline_present":
            entity(document, "plan_proposal")["data"]["plan_payload"]["deadline"] = ""
        elif check_id == "planning.stage_task_structure":
            entity(document, "plan_proposal")["data"]["plan_payload"]["stages"] = []
        elif check_id == "planning.core_task_evidence":
            entity(document, "plan_proposal")["data"]["plan_payload"]["stages"][0][
                "tasks"
            ][0]["evidence_required"] = False
        elif check_id == "planning.resource_snapshot":
            entity(document, "plan_proposal")["data"]["plan_payload"][
                "available_resources"
            ] = []
        elif check_id in {
            "intervention.guard_complete",
            "intervention.blocked_zero_effect",
        }:
            document["result"]["guard"]["status"] = "blocked"
        elif check_id == "intervention.allowed_chain":
            document["state_after"]["logical_entities"] = [
                item
                for item in document["state_after"]["logical_entities"]
                if item["entity_type"] != "outbox_receipt"
            ]
        elif check_id == "intervention.receipt_semantics":
            entity(document, "outbox_receipt")["data"]["status"] = "delivered"
        elif check_id == "intervention.replay_idempotent":
            document["isolation_evidence"]["recording_sink_attempts"] = 2
        elif check_id == "intervention.model_receipt_isolation":
            document["isolation_evidence"]["agent_observed_emulated_receipt"] = True
        elif check_id == "intervention.provider_calls_zero":
            document["isolation_evidence"]["smtp_calls"] = 1
        elif check_id == "assessment.submission_present":
            document["state_after"]["logical_entities"] = [
                item
                for item in document["state_after"]["logical_entities"]
                if item["entity_type"] != "submission"
            ]
        elif check_id == "assessment.score_threshold_verdict":
            entity(document, "submission")["data"]["status"] = "accepted"
        elif check_id == "assessment.missing_evidence_not_accept":
            document["result"]["action_class"] = "ACCEPT"
        elif check_id == "assessment.invocation_durable":
            invocation[0]["durable_status"] = "failed"
        elif check_id == "assessment.feedback_present":
            entity(document, "submission")["data"]["feedback"] = ""
        elif check_id == "assessment.operation_delta_alignment":
            document["observable_trace"]["operations"] = []
        elif check_id == "assessment.unrelated_plan_unchanged":
            plan_ref = entity(document, "plan")["logical_id"]
            change = dict(document["state_delta"]["changes"][0])
            change["entity_ref"] = plan_ref
            document["state_delta"]["changes"].append(change)
        elif check_id == "revision.expected_version":
            invocation[0]["canonical_args"]["expected_version"] += 1
        elif check_id == "revision.requested_scope":
            change = dict(document["state_delta"]["changes"][0])
            change["entity_ref"] = entity(document, "plan")["logical_id"]
            change["field_path"] = "data.goal"
            document["state_delta"]["changes"].append(change)
        elif check_id == "revision.reversible_patch":
            document["observable_trace"]["operations"][0]["inverse_patch"] = {}
        elif check_id == "revision.operation_delta_alignment":
            plan_ref = entity(document, "plan")["logical_id"]
            next(
                item
                for item in document["state_delta"]["changes"]
                if item["entity_ref"] == plan_ref
            )["operation_refs"] = []
        elif check_id == "revision.unrelated_state":
            run = entity(document, "agent_run")
            run["entity_type"] = "task"
        elif check_id == "revision.approval_boundary":
            invocation[0]["durable_status"] = "pending_approval"
        elif check_id == "revision.durable_success":
            document["result"]["layers"]["run_status"] = "failed"
        elif check_id == "trace.model_ordinals":
            document["observable_trace"]["model_turns"][1]["ordinal"] = 3
        elif check_id == "trace.tool_refs":
            document["observable_trace"]["model_turns"][0]["tool_call_refs"] = [
                "tool_invocation:missing"
            ]
        elif check_id == "trace.invocation_event_order":
            document["observable_trace"]["run_events"][0]["ordinal"] = 2
        elif check_id == "trace.result_digests":
            invocation[0]["result_digest"] = "0" * 64
        elif check_id == "trace.operation_patches":
            document["observable_trace"]["operations"] = (
                [
                    {
                        **document["observable_trace"]["operations"][0],
                        "patch_digest": "0" * 64,
                    }
                ]
                if document["observable_trace"]["operations"]
                else [
                    {
                        "patch_digest": "0" * 64,
                        "forward_patch": {},
                        "inverse_patch": {},
                    }
                ]
            )
        elif check_id == "trace.terminal_state":
            document["result"]["layers"]["run_status"] = "queued"
        elif check_id == "trace.no_tool_complete":
            document["observable_trace"]["tool_invocations"] = []
        elif check_id == "trace.checkpoint_independent":
            entity(document, "agent_run")["data"]["checkpoint"] = {"present": True}
        elif check_id == "isolation.temporary_database":
            document["environment"]["runtime"]["database_mode"] = "none"
        elif check_id == "isolation.production_database_denied":
            document["isolation_evidence"]["repository_runtime_data_access"] = True
        elif check_id == "isolation.network_mode":
            document["environment"]["isolation"]["network_access"] = (
                "model_provider_only"
            )
        elif check_id == "isolation.fake_outbox":
            document["environment"]["isolation"]["notification_mode"] = "disabled"
        elif check_id == "isolation.no_sqlite_publish":
            document["isolation_evidence"]["published_sqlite_files"] = 1
        elif check_id == "isolation.no_routing_material":
            document["isolation_evidence"]["routing_material_exported"] = True
        elif check_id == "isolation.background_disabled":
            document["isolation_evidence"]["background_services_started"] = True
        elif check_id == "isolation.external_calls_zero":
            document["isolation_evidence"]["subprocess_calls"] = 1
        else:  # pragma: no cover - the assertion below keeps this table closed.
            raise AssertionError(f"missing fail mutation for {check_id}")

    baseline_checks = {
        check["check_id"]: check
        for pack, episode_id in source_by_pack.items()
        for check in evaluate_rules(episodes[episode_id])["checks"]
        if check["rule_pack"] == pack
    }
    assert len(baseline_checks) == 52
    for check_id, baseline in baseline_checks.items():
        mutant = json.loads(json.dumps(episodes[source_by_pack[baseline["rule_pack"]]]))
        violate(check_id, mutant)
        target = next(
            item
            for item in evaluate_rules(mutant)["checks"]
            if item["check_id"] == check_id
        )
        assert target["status"] == "fail", check_id


def test_rules_separate_pass_fail_boundary_missing_and_hard_gate(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    episode = _load(output / "episodes" / "A-E1-MINI-001.json")

    def assessment_check(document: dict[str, Any]) -> dict[str, Any]:
        return next(
            check
            for check in evaluate_rules(document)["checks"]
            if check["check_id"] == "assessment.score_threshold_verdict"
        )

    assert assessment_check(episode)["status"] == "pass"
    boundary = json.loads(json.dumps(episode))
    invocation = boundary["observable_trace"]["tool_invocations"][0]
    submission = next(
        item
        for item in boundary["state_after"]["logical_entities"]
        if item["entity_type"] == "submission"
    )
    invocation["canonical_args"]["score"] = invocation["canonical_args"][
        "pass_threshold"
    ]
    submission["data"]["status"] = "accepted"
    assert assessment_check(boundary)["status"] == "pass"
    failed = json.loads(json.dumps(episode))
    next(
        item
        for item in failed["state_after"]["logical_entities"]
        if item["entity_type"] == "submission"
    )["data"]["status"] = "accepted"
    failed_result = evaluate_rules(failed)
    check = next(
        item
        for item in failed_result["checks"]
        if item["check_id"] == "assessment.score_threshold_verdict"
    )
    assert check["status"] == "fail"
    assert check["check_id"] in failed_result["hard_gates"]
    assert failed_result["status"] == "fail"
    assert any(item["status"] == "pass" for item in failed_result["checks"])
    missing = json.loads(json.dumps(episode))
    del missing["observable_trace"]["tool_invocations"][0]["canonical_args"][
        "pass_threshold"
    ]
    assert assessment_check(missing)["status"] == "invalid_input"
    assert evaluate_rules(missing)["status"] == "invalid_input"


def test_not_applicable_is_not_invalid_input(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    episode = _load(output / "episodes" / "P-E1-MINI-001.json")
    result = evaluate_rules(episode)
    intervention = [
        check for check in result["checks"] if check["rule_pack"] == "intervention"
    ]
    assert intervention
    assert {check["status"] for check in intervention} == {"not_applicable"}
    assert all(check["status"] != "invalid_input" for check in intervention)


def test_evaluate_rules_cli_is_atomic_filterable_and_byte_deterministic(
    e2_batch: tuple[Path, RunAgentSummary], tmp_path: Path
) -> None:
    runtime, _ = e2_batch
    first = tmp_path / "first"
    second = tmp_path / "second"
    evaluate_run_rules(input_path=runtime, output=first)
    evaluate_run_rules(input_path=runtime, output=second)
    assert _files(first) == _files(second)
    assert validate_dataset(first).ok
    by_track = tmp_path / "track"
    summary = evaluate_run_rules(input_path=runtime, output=by_track, track="revision")
    assert summary.episode_ids == ("R-E1-MINI-001",)
    by_episode = tmp_path / "episode"
    summary = evaluate_run_rules(
        input_path=runtime,
        output=by_episode,
        episode_ids={"I-E1-MINI-001"},
    )
    assert summary.episode_ids == ("I-E1-MINI-001",)
    marker_root = tmp_path / "existing"
    marker_root.mkdir()
    marker = marker_root / "preserve.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(RuleEvaluationError, match="output directory already exists"):
        evaluate_run_rules(input_path=runtime, output=marker_root)
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_evaluate_rules_cli_failure_is_structured_and_publishes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "not-published"
    code = cli_main(
        [
            "evaluate-rules",
            "--input",
            str(tmp_path / "missing"),
            "--output",
            str(output),
        ]
    )
    error = json.loads(capsys.readouterr().err)
    assert code == 1
    assert error == {
        "episode_id": "batch",
        "error_code": "input_invalid",
        "message": "Runtime output directory is invalid",
        "stage": "load",
        "status": "error",
    }
    assert not output.exists()


def test_evaluate_rules_cli_exit_codes_distinguish_invalid_and_failures(
    e2_batch: tuple[Path, RunAgentSummary],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, _ = e2_batch

    def variant(name: str, mutate: Any) -> Path:
        root = tmp_path / f"input-{name}"
        shutil.copytree(runtime, root)
        episode_path = root / "episodes/P-E1-MINI-001.json"
        episode = _load(episode_path)
        proposal = next(
            item
            for item in episode["state_after"]["logical_entities"]
            if item["entity_type"] == "plan_proposal"
        )
        mutate(proposal["data"]["plan_payload"])
        _rehash_episode(episode)
        episode_path.write_bytes(canonical_json_bytes(episode))
        manifest_path = root / "run-manifest.json"
        manifest = _load(manifest_path)
        manifest["episode_digests"]["P-E1-MINI-001"] = episode["provenance"][
            "episode_sha256"
        ]
        manifest["manifest_sha256"] = sha256_digest(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        )
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        assert validate_dataset(root).ok
        return root

    invalid = variant("invalid", lambda payload: payload.pop("deadline"))
    assert (
        cli_main(
            [
                "evaluate-rules",
                "--input",
                str(invalid),
                "--output",
                str(tmp_path / "invalid-output"),
            ]
        )
        == 1
    )
    assert (tmp_path / "invalid-output/rules/P-E1-MINI-001.json").is_file()
    assert "invalid_input_episodes=1" in capsys.readouterr().out

    hard_gate = variant(
        "hard-gate",
        lambda payload: payload["stages"][0]["tasks"][0].update(
            evidence_required=False
        ),
    )
    assert (
        cli_main(
            [
                "evaluate-rules",
                "--input",
                str(hard_gate),
                "--output",
                str(tmp_path / "hard-gate-output"),
            ]
        )
        == 2
    )
    assert "hard_gate_episodes=1" in capsys.readouterr().out

    noncritical = variant("noncritical", lambda payload: payload.update(deadline=""))
    assert (
        cli_main(
            [
                "evaluate-rules",
                "--input",
                str(noncritical),
                "--output",
                str(tmp_path / "noncritical-output"),
            ]
        )
        == 3
    )
    summary = capsys.readouterr().out
    assert "failed_episodes=1" in summary
    assert "hard_gate_episodes=0" in summary


def test_v2_and_rule_contracts_reject_unknown_fields(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    episode = _load(output / "episodes" / "P-E1-MINI-001.json")
    episode["unknown"] = True
    assert any(
        issue.code == "schema.unknown_field" for issue in validate_episode(episode)
    )
    valid_episode = _load(output / "episodes" / "P-E1-MINI-001.json")
    result = evaluate_rules(valid_episode)
    result["unknown"] = True
    with pytest.raises(ValueError):
        RuleResultV1.model_validate(result)


def test_nested_operation_patch_aligns_to_leaf_delta() -> None:
    target = "plan:test:nested"
    before = _snapshot(
        [_entity(target, "plan", {"preferences": {"pace": "steady"}}, 1)]
    )
    operation = _entity(
        "operation:test:nested",
        "operation",
        {
            "run_ref": "agent_run:test:001",
            "invocation_ref": None,
            "tool_name": "plan.patch",
            "primary_entity_type": "plan",
            "primary_entity_ref": target,
            "forward_patch": {"changes": {"preferences": {"pace": "focused"}}},
            "inverse_patch": {"changes": {"preferences": {"pace": "steady"}}},
            "status": "committed",
            "created_at": "2026-08-31T00:00:01.000000Z",
            "undone_at": None,
        },
        2,
    )
    after = _snapshot(
        [
            _entity(target, "plan", {"preferences": {"pace": "focused"}}, 1),
            operation,
        ]
    )
    delta = build_state_delta(before, after)
    change = next(
        item
        for item in delta["changes"]
        if item["field_path"] == "data.preferences.pace"
    )
    assert change["operation_alignment"] == "matched"
    assert change["operation_refs"] == ["operation:test:nested"]


def test_snapshot_data_allowlist_and_formal_boundary_fail_closed(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    episode = _load(output / "episodes/P-E1-MINI-001.json")
    for root in ("state_before", "state_after"):
        learner = next(
            item
            for item in episode[root]["logical_entities"]
            if item["entity_type"] == "learner"
        )
        learner["data"]["unsupported_public_field"] = "synthetic"
    _rehash_episode(episode)
    assert any(
        issue.code == "snapshot.entity_field_mismatch"
        for issue in validate_episode(episode)
    )

    raw_identity = _load(output / "episodes/P-E1-MINI-001.json")
    event = next(
        item
        for item in raw_identity["state_after"]["logical_entities"]
        if item["entity_type"] == "run_event"
    )
    event["data"]["payload"]["unexpected_id"] = 7
    assert any(
        issue.code == "snapshot.raw_identity_field"
        for issue in validate_episode(raw_identity)
    )

    noncanonical_time = _load(output / "episodes/P-E1-MINI-001.json")
    noncanonical_time["environment"]["frozen_time"] = "2026-08-31T09:00:00+08:00"
    noncanonical_time["environment"]["manifest_sha256"] = environment_manifest_digest(
        noncanonical_time["environment"]
    )
    noncanonical_time["provenance"]["episode_sha256"] = decision_episode_digest(
        noncanonical_time
    )
    assert any(
        issue.code == "environment.frozen_time_mismatch"
        for issue in validate_episode(noncanonical_time)
    )

    trace_mismatch = _load(output / "episodes/R-E1-MINI-001.json")
    learner_ref = next(
        item["logical_id"]
        for item in trace_mismatch["state_after"]["logical_entities"]
        if item["entity_type"] == "learner"
    )
    trace_mismatch["observable_trace"]["operations"][0]["affected_entity_refs"].append(
        learner_ref
    )
    trace_mismatch["observable_trace"]["tool_invocations"][0]["execution_status"] = (
        "not_executed"
    )
    assert any(
        issue.code == "trace.operation_snapshot_mismatch"
        for issue in validate_episode(trace_mismatch)
    )
    assert any(
        issue.code == "trace.invocation_snapshot_mismatch"
        for issue in validate_episode(trace_mismatch)
    )

    formal = _load(output / "episodes/P-E1-MINI-001.json")
    formal["provenance"]["formal_evaluation_result"] = True
    formal["provenance"]["evaluation_status"] = "formal_model_evaluation"
    formal["result"]["layers"]["formal_evaluation_eligibility"] = "eligible"
    formal["provenance"]["episode_sha256"] = decision_episode_digest(formal)
    codes = {issue.code for issue in validate_episode(formal)}
    assert "provenance.stub_formal_claim" in codes
    assert "provenance.formal_eligibility_mismatch" in codes


def test_runtime_and_rule_manifests_are_closed_inventories(
    e2_batch: tuple[Path, RunAgentSummary], tmp_path: Path
) -> None:
    runtime, _ = e2_batch
    mixed = tmp_path / "mixed-runtime"
    shutil.copytree(runtime, mixed)
    shutil.copy2(
        DATASET / "episodes/mini/P-MINI-001.json",
        mixed / "episodes/P-MINI-001.json",
    )
    assert any(
        issue.code == "manifest.episode_inventory_mismatch"
        for issue in validate_dataset(mixed).issues
    )

    rules = tmp_path / "rules"
    evaluate_run_rules(input_path=runtime, output=rules)
    manifest = _load(rules / "rule-manifest.json")
    first_id = manifest["episode_ids"][0]
    manifest["rule_result_digests"][first_id] = "f" * 64
    manifest["manifest_sha256"] = sha256_digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    (rules / "rule-manifest.json").write_bytes(canonical_json_bytes(manifest))
    assert any(
        issue.code == "manifest.rule_result_mismatch"
        for issue in validate_dataset(rules).issues
    )


def test_final_delivery_status_is_distinct_from_tool_observation(
    e2_batch: tuple[Path, RunAgentSummary],
) -> None:
    output, _ = e2_batch
    episode = _load(output / "episodes/I-E1-MINI-001.json")
    invocation = episode["observable_trace"]["tool_invocations"][0]
    assert invocation["observation_status"] == "pending_delivery"
    assert invocation["durable_status"] == "committed"
    assert episode["result"]["layers"]["durable_status"] == "committed"
    assert episode["isolation_evidence"]["agent_observed_pending_delivery"] is True
    assert episode["isolation_evidence"]["agent_observed_emulated_receipt"] is False


def test_failed_durable_tool_is_not_misclassified_as_no_op() -> None:
    invocation = _entity(
        "tool_invocation:test:failure",
        "tool_invocation",
        {"status": "needs_reconciliation"},
        1,
    )
    after = {"logical_entities": [invocation]}
    trace = {
        "tool_invocations": [
            {
                "invocation_id": invocation["logical_id"],
                "tool_name": "plan.patch",
                "execution_status": "completed",
                "durable_status": "needs_reconciliation",
                "result": {},
            }
        ],
        "operations": [],
    }
    effects, action, guard, _ = _effects_and_action(
        episode_id="E2-FAILURE-001",
        after=after,
        delta={"changes": []},
        trace=trace,
    )
    assert action == "APPLY_REVERSIBLE_PATCH"
    assert guard == "not_evaluated"
    assert effects[0]["effect_type"] == "failed"
    assert effects[0]["status"] == "pending"


def test_multiple_guard_decisions_keep_allowed_and_blocked_effects_separate() -> None:
    first_invocation = _entity(
        "tool_invocation:test:allowed", "tool_invocation", {"status": "committed"}, 1
    )
    second_invocation = _entity(
        "tool_invocation:test:blocked", "tool_invocation", {"status": "rejected"}, 2
    )
    decision_allowed = _entity(
        "proactive_decision:test:allowed",
        "proactive_decision",
        {
            "outcome": "success_intervention",
            "policy_version": "policy:test:v1",
            "invocation_ref": first_invocation["logical_id"],
        },
        3,
    )
    decision_blocked = _entity(
        "proactive_decision:test:blocked",
        "proactive_decision",
        {
            "outcome": "guard_rejected",
            "policy_version": "policy:test:v1",
            "invocation_ref": second_invocation["logical_id"],
        },
        4,
    )
    notification = _entity(
        "notification:test:allowed",
        "notification",
        {"invocation_ref": first_invocation["logical_id"]},
        5,
    )
    after = {
        "logical_entities": [
            first_invocation,
            second_invocation,
            decision_allowed,
            decision_blocked,
            notification,
        ]
    }
    trace = {
        "tool_invocations": [
            {
                "invocation_id": first_invocation["logical_id"],
                "tool_name": "notification.send",
                "execution_status": "completed",
                "durable_status": "committed",
                "result": {},
            },
            {
                "invocation_id": second_invocation["logical_id"],
                "tool_name": "notification.send",
                "execution_status": "blocked",
                "durable_status": "rejected",
                "result": {},
            },
        ],
        "operations": [],
    }
    delta = {
        "changes": [
            {
                "entity_ref": notification["logical_id"],
                "operation_refs": [],
            }
        ]
    }
    effects, action, guard, _ = _effects_and_action(
        episode_id="E2-MULTI-GUARD-001",
        after=after,
        delta=delta,
        trace=trace,
    )
    assert action == "INTERVENE_MESSAGE"
    assert guard == "mixed"
    assert [item["status"] for item in effects] == ["applied", "blocked"]
    attempts = [
        {
            "attempt_id": f"attempt:test:{ordinal}",
            "invocation_refs": [invocation["logical_id"]],
        }
        for ordinal, invocation in enumerate((first_invocation, second_invocation), 1)
    ]
    guards = _guard_decisions(
        episode_id="E2-MULTI-GUARD-001",
        facts=_guard_facts(after, trace),
        attempts=attempts,
        effects=effects,
    )
    assert [item["status"] for item in guards] == ["allowed", "blocked"]
    assert guards[0]["final_effect_refs"] == [effects[0]["effect_id"]]
    assert guards[1]["final_effect_refs"] == [effects[1]["effect_id"]]
    assert (
        _durable_status(
            after=after,
            trace=trace,
            run_status="completed",
            guard_status=guard,
            effects=effects,
        )
        == "partial"
    )
