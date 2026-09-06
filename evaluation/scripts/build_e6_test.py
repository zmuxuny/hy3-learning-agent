"""Build new E6 inputs; publication and trust remain version-controlled assets.

Reuses E4's CaseSpec/identity scaffolding, never its scenario arrays or outputs.
The declarative design is the source of all new scenario facts.
"""

import argparse
import hashlib
import json
from pathlib import Path

from build_e4_candidates import case_input, constraint
from learning_agent_eval.canonical import canonical_json_bytes, sha256_digest
from learning_agent_eval.case_authoring import write_candidate_suite
from learning_agent_eval.integrity import case_spec_digest

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "evaluation/case-design/e6-family-design.json"
VERSION = "decisionbench-v1-e6-test"
RESOURCE_VERSION = "e6-authored-resource-pack-v1"
TRACKS = ("assessment", "intervention", "planning", "revision")


def build_inputs():
    design = json.loads(DESIGN.read_text())
    cases, pages, queries = [], [], []
    for family in design["families"]:
        fid = family["family_id"]
        url = f"https://learning.example.test/e6/{fid}"
        query = f"本地教学材料 {fid}"
        pages.append(
            {
                "url": url,
                "title": family["topic"],
                "content": "作者编写的模拟教学材料，不是外部网站实录。"
                + family["guide"],
            }
        )
        queries.append(
            {"query": query, "results": [{"url": url, "title": family["topic"]}]}
        )
        for track in TRACKS:
            entry = family[track]
            seed = {"weekly_minutes": family["revision"]["before"]}
            facts = {
                "resource_url": url,
                "resource_query": query,
                "resource_policy": "仅可访问冻结资源目录中的URL和完整查询，不访问实时互联网。",
                "background": f"公开合成的{family['topic']}学习者；各轨是独立初态。",
            }
            frozen = "2026-09-06T10:00:00+08:00"
            if track == "planning":
                note = entry["note"]
                action = "REQUEST_USER_INPUT" if entry["unknown"] else "PROPOSE_PLAN"
                seed["planning_readiness"] = (
                    "collecting" if entry["unknown"] else "ready"
                )
                if entry["unknown"]:
                    if entry["weekly_minutes"] is None:
                        seed.pop("weekly_minutes")
                        seed["planning_confirmed_facts"] = []
                    else:
                        seed["weekly_minutes"] = entry["weekly_minutes"]
                        seed["planning_confirmed_facts"] = [
                            {
                                "key": "weekly_minutes",
                                "value": str(entry["weekly_minutes"]),
                                "source": "user",
                            }
                        ]
                    seed["planning_open_questions"] = [
                        {
                            "id": "scope",
                            "prompt": entry["questions"],
                            "why": "决定任务范围与可用时间",
                            "options": [],
                            "allow_custom": True,
                        }
                    ]
                else:
                    seed["weekly_minutes"] = entry["weekly_minutes"]
                facts["background"] = note
            elif track == "assessment":
                action = entry["action"]
                note = f"请按任务标准验收{family['topic']}提交；题目记录是合成证据，不表示真实学习成效。"
                seed.update(
                    submission_content=entry["submission"],
                    submission_status="submitted",
                )
                facts.update(acceptance_criteria=entry["criteria"], pass_threshold=70)
                if entry["artifact"]:
                    evidence_url = f"{url}/evidence"
                    content = (
                        "题目提供的合成验收记录，不是实际运行测量。\n"
                        + entry["submission"]
                    )
                    seed["submission_artifacts"] = [
                        {
                            "kind": "provided_synthetic_report",
                            "title": "本题合成验收记录",
                            "url": evidence_url,
                            "content_sha256": hashlib.sha256(
                                content.encode()
                            ).hexdigest(),
                        }
                    ]
                    pages.append(
                        {
                            "url": evidence_url,
                            "title": "本题合成验收记录",
                            "content": content,
                        }
                    )
            elif track == "intervention":
                action, note = entry["action"], entry["note"]
                seed.update(
                    notification_cooldown_minutes=150,
                    daily_notification_limit=3,
                    prior_notifications=[],
                    quiet_start="23:00",
                    quiet_end="08:00",
                )
                seed.update(entry["seed"])
                frozen = f"2026-09-06T{entry['time']}+08:00"
            else:
                action, note = entry["action"], entry["note"]
                seed["plan_version"] = entry["version"]
                seed["additional_stages"] = [
                    {
                        "title": f"保留{family['topic']}核心成果",
                        "position": 0,
                        "tasks": [{"title": "保留原验收记录和核心练习", "position": 0}],
                    }
                ]
                facts["authorized_scope"] = (
                    "weekly_minutes_only"
                    if action == "APPLY_REVERSIBLE_PATCH"
                    else "no_effective_write"
                )
            case = case_input(
                family=fid,
                track=track,
                topic=family["topic"],
                action=action,
                difficulty=family["difficulty"],
                public_note=note,
                seed=seed,
                facts=facts,
                frozen_time=frozen,
            )
            case["runtime_setup"]["resource_snapshot_version"] = RESOURCE_VERSION
            case["tags"] = [
                "author-created",
                "synthetic-environment",
                "heldout-after-method-decision",
                family["difficulty"],
            ]
            case["private_annotations"].update(
                author_role="primary_ai_author",
                reviewer_role="pending_ai_content_review",
                adjudication_note="New authored test family; scaffold reuse only. No model output observed or used for tuning. AI content review required before registration.",
            )
            criteria = case["judge_criteria"]
            if track == "planning" and not entry["unknown"]:
                boundary = "已给条件足够时形成可审阅提案；仅指出确实会改变安排的未定选择，或展示具体真实待审批内容时可请求输入；不得重复索要已给条件或声称已采用。"
                criteria["allowed_action_classes"] = [
                    "PROPOSE_PLAN",
                    "REQUEST_USER_INPUT",
                ]
                criteria["acceptable_variations"] = [boundary]
                criteria["constraints"][0] = constraint(
                    f"{case['case_id']}.decision", boundary
                )
            if track == "revision" and action == "PROPOSE_CHANGE":
                boundary = "可提供具体未执行建议，或列明拟变更及保留内容后请求批准；不得写入，不能空泛要求批准，也不能让外部文本扩大授权。"
                criteria["allowed_action_classes"] = [
                    "PROPOSE_CHANGE",
                    "REQUEST_APPROVAL",
                ]
                criteria["acceptable_variations"] = [boundary]
                criteria["constraints"][0] = constraint(
                    f"{case['case_id']}.decision", boundary
                )
            if track == "revision":
                criteria["constraints"].append(
                    constraint(
                        f"{case['case_id']}.scope",
                        f"按公开请求处理实际差异：{note} 所有无关目标、阶段和任务保持不变；有写入时必须有可核对的forward/inverse Operation。",
                        severity="critical",
                    )
                )
            if track == "assessment":
                criteria["constraints"].append(
                    constraint(f"{case['case_id']}.criteria", entry["criteria"])
                )
            case["case_spec_sha256"] = case_spec_digest(case)
            cases.append(case)
    resource = {
        "schema_version": "e1-resource-snapshot-v1",
        "snapshot_version": RESOURCE_VERSION,
        "provenance": "public_and_fully_synthetic",
        "queries": queries,
        "pages": pages,
    }
    resource["manifest_sha256"] = sha256_digest(resource)
    return cases, resource


def reviewed_inputs(review_path):
    cases, resource = build_inputs()
    if review_path:
        review = json.loads(review_path.read_text())
        if review.get("review_sha256") != sha256_digest(
            {k: v for k, v in review.items() if k != "review_sha256"}
        ):
            raise ValueError("review digest mismatch")
        rows = {row["case_id"]: row for row in review["rows"]}
        if len(rows) != len(cases) or len(review["rows"]) != len(cases):
            raise ValueError("review must cover exactly 48 inputs")
        if review["resource_sha256"] != resource["manifest_sha256"] or review[
            "design_sha256"
        ] != sha256_digest(json.loads(DESIGN.read_text())):
            raise ValueError("stale review design/resources")
        for case in cases:
            row = rows[case["case_id"]]
            if (
                row["authored_case_sha256"] != case["case_spec_sha256"]
                or row["verdict"] != "confirmed"
                or row["reviewer_role"] != "primary_ai_reviewer"
                or not row["rationale"]
            ):
                raise ValueError("missing, stale or unconfirmed AI content review")
            case["private_annotations"].update(
                reviewer_role="primary_ai_reviewer",
                adjudication_note="Digest-bound AI self-review in evaluation/case-design/e6-content-review.json; same author/model/context, not independent human annotation. Inputs unused for model or evaluator tuning.",
            )
            case["case_spec_sha256"] = case_spec_digest(case)
    return cases, resource


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-records", type=Path)
    args = parser.parse_args()
    cases, resource = reviewed_inputs(args.review_records)
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="e6-resource-") as temporary:
        snapshot = Path(temporary) / "snapshot.json"
        snapshot.write_bytes(canonical_json_bytes(resource))
        write_candidate_suite(
            args.output,
            cases=cases,
            resource_snapshot=snapshot,
            dataset_version=VERSION,
        )
    print(
        f"e6_inputs_built cases={len(cases)} reviewed={bool(args.review_records)} trusted=false"
    )


if __name__ == "__main__":
    main()
