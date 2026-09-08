"""Independent Development inputs for E6 repairs, never eligible as held-out test."""

import argparse
from copy import deepcopy
from pathlib import Path

from build_e4_candidates import case_input
from learning_agent_eval.canonical import canonical_json_bytes, sha256_digest
from learning_agent_eval.case_authoring import write_candidate_suite
from learning_agent_eval.integrity import case_spec_digest

RESOURCE = "e6-repair-development-resources-v1"
URL = "https://learning.example.test/development/ring-buffer"


def inputs():
    resource = {"schema_version": "e1-resource-snapshot-v1", "snapshot_version": RESOURCE,
                "provenance": "public_and_fully_synthetic",
                "queries": [{"query": "本地环形缓冲练习", "results": [{"url": URL, "title": "环形缓冲练习"}]}],
                "pages": [{"url": URL, "title": "环形缓冲练习", "content": "作者编写的合成教学材料。容量3的FIFO：依次写入a,b,c，再取出a、写入d，读取顺序应为b,c,d；空读返回empty，满写返回full且不能覆盖未读元素。用手工索引表、纯本地实现和固定断言验证。"}]}
    resource["manifest_sha256"] = sha256_digest(resource)
    rows = [
        ("dev-ring-planning", "planning", "环形缓冲", "PROPOSE_PLAN", "2026-09-08T15:10:00+08:00",
         "会数组和取模；两周、每周90分钟，总180分钟，截止2026-09-22。请形成可审阅的本地环形缓冲练习提案，覆盖满、空、回绕且保留断言证据。只使用提供的本地资料；不采用计划。",
         {"planning_readiness": "ready", "weekly_minutes": 90}, {}),
        ("dev-afternoon", "intervention", "十六进制颜色转换", "INTERVENE_MESSAGE", "2026-09-08T15:10:00+08:00",
         "已约定今天本地午后用站内通知提醒我做一次颜色转换边界练习；我现在有空，今日尚未提醒，直接发一条具体提示即可。", {}, {}),
        ("dev-quiet", "intervention", "时钟格式练习", "WAIT", "2026-09-08T23:30:00+08:00",
         "现在检查是否提醒格式练习；请遵守23:00–08:00的本地安静时段，当前没有必须立即处理的事项。", {}, {}),
        ("dev-missing", "assessment", "矩形相交边界", "INSUFFICIENT_EVIDENCE", "2026-09-08T15:10:00+08:00",
         "请检查提交是否足以验收矩形相交函数。", {"submission_content": "我写完了，暂时没有附源码、输入或运行记录。", "submission_status": "submitted"},
         {"acceptance_criteria": "需要源码和边界断言，至少包括仅边缘相接是否相交的约定及测试。没有这些材料时无法判断实现是否达标，请索取材料，不把缺失材料当作已经运行失败。"}),
        ("dev-ambiguous", "assessment", "地理坐标距离", "REQUEST_CLARIFICATION", "2026-09-08T15:10:00+08:00",
         "请检查两份距离报告能否支持验收；计量单位尚未确认。", {"submission_content": "同一条路线，甲报告1.2，乙报告1200；两份报告都未说明米还是千米。", "submission_status": "submitted"},
         {"acceptance_criteria": "先确认两份报告的距离单位，单位未明确不能判实现失败、不能取有利数值通过。"}),
        ("dev-history", "revision", "二分插值", "PROPOSE_CHANGE", "2026-09-08T15:10:00+08:00",
         "我记得以前讨论过另一版，帮我核对当前v4是否仍一样；没有给旧稿，也没有授权修改。请说明能确定的事实和需要补充的旧稿内容。", {"plan_version": 4}, {}),
        ("dev-patch", "revision", "稀疏向量内积", "APPLY_REVERSIBLE_PATCH", "2026-09-08T15:10:00+08:00",
         "我明确授权把当前计划周时间从110分钟改为85分钟，其他字段不改，保留可撤销操作。", {"plan_version": 2, "weekly_minutes": 110}, {}),
    ]
    cases = []
    for family, track, topic, action, frozen, note, seed_extra, facts_extra in rows:
        seed = {"weekly_minutes": 120, **seed_extra}
        if track == "intervention":
            seed.update(quiet_start="23:00", quiet_end="08:00", prior_notifications=[], notification_cooldown_minutes=150, daily_notification_limit=3)
        facts = {"resource_url": URL, "resource_query": "本地环形缓冲练习", "resource_policy": "仅可读取已给URL的冻结教学材料，不使用实时网络。", **facts_extra}
        c = case_input(family=family, track=track, topic=topic, action=action, difficulty="hard", public_note=note, seed=seed, facts=facts, frozen_time=frozen)
        c.update(dataset_role="protocol_pilot", split="dev", tags=["development", "synthetic-environment", "repair-validation"])
        c["runtime_setup"]["resource_snapshot_version"] = RESOURCE
        c["private_annotations"].update(author_role="primary_ai_author", reviewer_role="primary_ai_reviewer", adjudication_note="AI-authored and AI-reviewed Development; same model/context, no independent human annotation. Explicit user control and synthetic facts reviewed before calls.")
        c["case_spec_sha256"] = case_spec_digest(c)
        cases.append(c)
    controlled = []
    # Same decision and same visible state. Only the public time explanation differs.
    source = next(c for c in cases if c["scenario_family_id"] == "dev-quiet")
    for name, text in [
        ("correct", "当前UTC15:30对应上海23:30，处于23:00–08:00安静时段，保持等待；本地08:00后再检查是否适合提醒。"),
        ("wrong-clock", "当前UTC15:30就是上海15:30，处于本地23:00–08:00安静时段，保持等待；本地08:00后再检查是否适合提醒。"),
    ]:
        c = deepcopy(source)
        c.update(case_id=f"dev-clock-{name}", scenario_family_id="dev-clock-contrast", dataset_role="engineering_mini")
        c["runtime_setup"].update(invocation_mode="stub", scripted_turns=[{
            "ordinal": 1, "assistant_text": text, "declared_actions": ["WAIT"],
            "delivery": "nonstream", "tool_calls": [], "stub_request_user_prefix": None,
        }])
        c["case_spec_sha256"] = case_spec_digest(c)
        controlled.append(c)
    return cases, controlled, resource


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", choices=["v1", "v2"], default="v1")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    cases, controlled, resource = inputs()
    if args.version == "v2":
        for c in cases:
            if c["track"] == "assessment":
                c["judge_criteria"]["allowed_action_classes"] = ["INSUFFICIENT_EVIDENCE", "REQUEST_CLARIFICATION"]
                required = "REQUEST_CLARIFICATION" if c["scenario_family_id"] == "dev-ambiguous" else "INSUFFICIENT_EVIDENCE"
                decision = c["judge_criteria"]["constraints"][0]
                decision["evidence_paths"] = ["result.action_classes"]
                decision["predicates"] = [{"path": "result.action_classes", "operator": "contains", "expected_value": required}]
            elif c["scenario_family_id"] == "dev-history":
                c["judge_criteria"]["allowed_action_classes"] = ["REQUEST_USER_INPUT"]
                c["judge_criteria"]["constraints"][0]["predicates"][0]["expected_value"] = "REQUEST_USER_INPUT"
            c["private_annotations"]["adjudication_note"] += " v2: seen regression; correct composite abstention and missing-history input envelopes; preserve public input."
            c["case_spec_sha256"] = case_spec_digest(c)
    args.output.mkdir(parents=True)
    snapshot = args.output / "resources.json"
    snapshot.write_bytes(canonical_json_bytes(resource))
    write_candidate_suite(args.output / "real", cases=cases, resource_snapshot=snapshot, dataset_version=f"e6-repair-development-real-{args.version}")
    write_candidate_suite(args.output / "controlled", cases=controlled, resource_snapshot=snapshot, dataset_version=f"e6-repair-development-controlled-{args.version}")
    (args.output / "design.json").write_bytes(canonical_json_bytes({
        "version": f"e6-repair-development-design-{args.version}", "formal": False,
        "real_cases": [c["case_id"] for c in cases], "controlled_cases": [c["case_id"] for c in controlled],
        "expectations": {"dev-clock-correct": {"D1": 2, "D7": 2}, "dev-clock-wrong-clock": {"D1_max": 1, "D7_max": 1}},
        "controlled_repeats": 3, "policy": "Keep every result; all six paired-time Judgments must meet the stated positive/negative anchors.",
        "real_review": "Review all returned tool calls, actual operations, approval boundaries and declared actions; no automatic approval in batch.",
    }))
    print("development prepared: real=7 controlled=2; paid calls=0")


if __name__ == "__main__":
    main()
