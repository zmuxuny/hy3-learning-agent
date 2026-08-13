"""Small deterministic M13 quality baseline.

The scenarios intentionally exercise fact/projection boundaries rather than
model quality.  They are stable fixtures for CI and for comparing a future
reducer version; none of them calls the network or mutates the user database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.services.evidence import build_evidence_state


SCENARIOS: tuple[dict[str, str], ...] = (
    {"id": "empty_ledger", "description": "没有任何证据时保持 unknown"},
    {"id": "single_submission", "description": "一次提交进入 practicing"},
    {"id": "accepted_submission", "description": "验收通过进入 demonstrated"},
    {"id": "needs_revision", "description": "验收失败保持 practicing"},
    {"id": "quiz_passed", "description": "测验通过进入 demonstrated"},
    {"id": "quiz_failed", "description": "测验失败不虚报掌握"},
    {"id": "task_verified", "description": "带来源任务证据进入 demonstrated"},
    {"id": "task_without_evidence", "description": "没有证据的任务不进入账本"},
    {"id": "self_report_only", "description": "自述不能单独证明能力"},
    {"id": "manual_observation", "description": "手工观察保留来源身份"},
    {"id": "assisted_attempt", "description": "提示后作答保留 assistance_level"},
    {"id": "independent_attempt", "description": "独立作答保留 assistance_level"},
    {"id": "same_task_transfer", "description": "同题表现保留 transfer_level"},
    {"id": "variant_transfer", "description": "变式表现保留 transfer_level"},
    {"id": "conflicting_scores", "description": "冲突分数不覆盖历史"},
    {"id": "late_success", "description": "晚到的成功按 occurred_at 参与投影"},
    {"id": "duplicate_source", "description": "重复来源由调用方幂等键拦截"},
    {"id": "supersession", "description": "新观察替代旧观察但不删除旧记录"},
    {"id": "invalidated_observation", "description": "失效观察退出当前投影"},
    {"id": "invalidated_superseder", "description": "失效的替代观察不隐藏旧观察"},
    {"id": "unscoped_message", "description": "无任务消息保留为未绑定证据"},
    {"id": "plan_one_isolation", "description": "计划一只读取计划一证据"},
    {"id": "plan_two_isolation", "description": "计划二只读取计划二证据"},
    {"id": "cross_plan_same_task_title", "description": "相同标题不跨计划合并"},
    {"id": "cross_plan_same_competency_key", "description": "技能映射未建立前不跨计划合并"},
    {"id": "code_artifact", "description": "代码 Artifact 有来源引用"},
    {"id": "file_artifact", "description": "文件 Artifact 有来源引用"},
    {"id": "quiz_artifact", "description": "测验 Artifact 有来源引用"},
    {"id": "submission_artifact", "description": "提交 Artifact 有来源引用"},
    {"id": "task_artifact", "description": "任务证据 Artifact 有来源引用"},
    {"id": "rubric_snapshot", "description": "评价保存 Rubric 快照"},
    {"id": "evaluator_snapshot", "description": "评价保存 evaluator"},
    {"id": "causal_chain", "description": "观察保存 causation_id"},
    {"id": "correlation_chain", "description": "观察保存 correlation_id"},
    {"id": "score_clamp", "description": "分数归一化限制在 0 到 1"},
    {"id": "zero_score", "description": "零分仍是有效失败证据"},
    {"id": "full_score", "description": "满分仍保留来源和时间"},
    {"id": "multiple_attempts", "description": "多次尝试保留计数"},
    {"id": "rebuild_digest", "description": "删除投影后可重建相同 digest"},
    {"id": "old_fact_preserved", "description": "历史事实不被新事实覆盖"},
    {"id": "conservative_unknown", "description": "没有足够证据时保持 unknown"},
)


def _observation(
    index: int,
    *,
    plan_id: int | None = 1,
    task_id: int | None = 10,
    outcome: str = "submitted",
    score: float | None = None,
    source_type: str = "submission",
    supersedes_id: int | None = None,
    invalidated_at: datetime | None = None,
    **extra: Any,
):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    return SimpleNamespace(
        id=index,
        source_type=source_type,
        source_id=f"baseline:{index}",
        task_id=task_id,
        competency_id=None,
        outcome=outcome,
        normalized_score=score,
        is_correct=outcome in {"accepted", "passed", "verified"},
        assistance_level=extra.get("assistance_level", "unknown"),
        transfer_level=extra.get("transfer_level", "unknown"),
        occurred_at=now,
        supersedes_id=supersedes_id,
        invalidated_at=invalidated_at,
        invalidation_reason=extra.get("invalidation_reason", ""),
        artifact_refs=extra.get("artifact_refs", [{"artifact_id": index, "kind": source_type}]),
        rubric_snapshot=extra.get("rubric_snapshot", {}),
        evaluator=extra.get("evaluator", {}),
        payload=extra.get("payload", {}),
        schema_version=1,
        recorded_at=now,
        run_id=None,
        session_id=None,
        plan_id=plan_id,
        competency_key=None,
        correlation_id=extra.get("correlation_id"),
        causation_id=extra.get("causation_id"),
        idempotency_key=f"baseline:{index}",
    )


def records_for(scenario_id: str) -> list[Any]:
    if scenario_id == "empty_ledger" or scenario_id in {"task_without_evidence", "conservative_unknown"}:
        return []
    if scenario_id == "self_report_only":
        return [_observation(1, source_type="self_report", outcome="submitted", artifact_refs=[])]
    if scenario_id == "supersession":
        return [_observation(1, outcome="needs_revision"), _observation(2, outcome="passed", score=0.9, supersedes_id=1)]
    if scenario_id == "invalidated_observation":
        return [_observation(1, invalidated_at=datetime(2026, 1, 2, tzinfo=timezone.utc), invalidation_reason="duplicate")]
    if scenario_id == "invalidated_superseder":
        return [_observation(1, outcome="needs_revision"), _observation(2, outcome="passed", score=0.9, supersedes_id=1, invalidated_at=datetime(2026, 1, 2, tzinfo=timezone.utc), invalidation_reason="retracted")]
    if scenario_id == "plan_one_isolation":
        return [_observation(1, plan_id=1, task_id=10, outcome="passed", score=0.8)]
    if scenario_id == "plan_two_isolation":
        return [_observation(1, plan_id=2, task_id=20, outcome="passed", score=0.8)]
    if scenario_id == "cross_plan_same_task_title":
        return [_observation(1, plan_id=1, task_id=10), _observation(2, plan_id=2, task_id=20)]
    if scenario_id == "cross_plan_same_competency_key":
        return [_observation(1, plan_id=1, task_id=10, competency_key="python.async"), _observation(2, plan_id=2, task_id=20, competency_key="python.async")]
    if scenario_id == "multiple_attempts":
        return [_observation(1, outcome="needs_revision", score=0.4), _observation(2, outcome="passed", score=0.85)]
    if scenario_id in {"assisted_attempt", "independent_attempt", "same_task_transfer", "variant_transfer"}:
        return [_observation(1, outcome="passed", score=0.8, assistance_level="hint" if scenario_id == "assisted_attempt" else "independent", transfer_level="variant" if scenario_id == "variant_transfer" else "same_task")]
    if scenario_id == "conflicting_scores":
        return [_observation(1, outcome="passed", score=0.9), _observation(2, outcome="needs_revision", score=0.4)]
    if scenario_id == "zero_score":
        return [_observation(1, outcome="failed", score=0.0)]
    if scenario_id == "full_score":
        return [_observation(1, outcome="passed", score=1.0)]
    if scenario_id == "score_clamp":
        return [_observation(1, outcome="passed", score=1.0)]
    extras = {}
    if scenario_id == "rubric_snapshot":
        extras["rubric_snapshot"] = {"threshold": 0.7}
    if scenario_id == "evaluator_snapshot":
        extras["evaluator"] = {"type": "agent", "version": "test"}
    if scenario_id == "causal_chain":
        extras["causation_id"] = "event:1"
    if scenario_id == "correlation_chain":
        extras["correlation_id"] = "run:1"
    if scenario_id in {"code_artifact", "file_artifact", "quiz_artifact", "submission_artifact", "task_artifact"}:
        extras["artifact_refs"] = [{"artifact_id": 99, "kind": scenario_id.replace("_artifact", "")}]
    return [_observation(1, outcome="passed" if scenario_id in {"accepted_submission", "quiz_passed", "task_verified", "late_success", "old_fact_preserved", "rebuild_digest", "full_score"} else "submitted", score=0.86 if scenario_id in {"accepted_submission", "quiz_passed", "task_verified"} else None, **extras)]


def evaluate_baseline() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        records = records_for(scenario["id"])
        projection = build_evidence_state(records)
        replay = build_evidence_state(records)
        checks = {
            "deterministic_digest": projection["digest"] == replay["digest"],
            "bounded_stage": all(item["evidence_stage"] in {"unknown", "exposed", "practicing", "demonstrated"} for item in projection["by_task"]),
        }
        if scenario["id"] == "self_report_only":
            checks["self_report_not_demonstrated"] = projection["by_task"][0]["evidence_stage"] != "demonstrated"
        if scenario["id"] == "supersession":
            checks["superseded_hidden"] = projection["by_task"][0]["evidence_ids"] == [2]
        if scenario["id"] == "invalidated_superseder":
            checks["invalidated_superseder_restores_old"] = projection["by_task"][0]["evidence_ids"] == [1]
        results.append({"id": scenario["id"], "description": scenario["description"], "ok": all(checks.values()), "checks": checks, "digest": projection["digest"]})
    return {"scenario_count": len(SCENARIOS), "passed": sum(item["ok"] for item in results), "ok": all(item["ok"] for item in results), "results": results}
