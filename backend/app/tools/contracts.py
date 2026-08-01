from typing import Any

from pydantic import BaseModel, ConfigDict, create_model


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="allow")


def output(name: str, **fields: type) -> type[BaseModel]:
    return create_model(
        name,
        __base__=ToolOutput,
        **{field: (annotation, ...) for field, annotation in fields.items()},
    )


TOOL_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "profile_get": output("ProfileGetOutput", agent_style=str, preferences=dict, quiet_hours=dict, xp=int, level=int, streak_days=int),
    "plan_list": output("PlanListOutput", plans=list),
    "plan_get": output("PlanGetOutput", id=int, title=str, status=str, progress=float, version=int, stages=list),
    "plan_create": output("PlanCreateOutput", plan_id=int, title=str, stage_count=int, operation_id=str),
    "task_patch": output("TaskPatchOutput", task_id=int, status=str, operation_id=str, undo_available=bool),
    "review_schedule": output("ReviewScheduleOutput", review_id=int, due_at=str, operation_id=str),
    "quiz_create": output("QuizCreateOutput", quiz_id=int, status=str, prompt=str, operation_id=str, undo_available=bool),
    "quiz_get": output("QuizGetOutput", quiz_id=int, plan_id=int, prompt=str, rubric=dict, status=str),
    "quiz_grade": output("QuizGradeOutput", quiz_id=int, score=float, status=str, operation_id=str, undo_available=bool),
    "memory_propose": output("MemoryProposalOutput", memory_id=int, status=str, approval_required=bool),
    "notification_send": output("NotificationSendOutput", blocked=bool, notifications=list),
    "planning_intake_get": output("PlanningIntakeGetOutput", exists=bool, session_id=str, goal=str, confirmed_facts=list, open_questions=list, readiness=str, readiness_confidence=float, rationale=str),
    "planning_intake_update": output("PlanningIntakeUpdateOutput", exists=bool, session_id=str, goal=str, confirmed_facts=list, open_questions=list, readiness=str, readiness_confidence=float, rationale=str),
    "planning_delegate": output("PlanningDelegateOutput", reports=list),
    "plan_proposal_create": output("PlanProposalCreateOutput", proposal_id=str, title=str, status=str, stage_count=int, task_count=int, approval_required=bool),
    "plan_patch": output("PlanPatchOutput", plan_id=int, version=int, operation_id=str, undo_available=bool),
    "stage_create": output("StageCreateOutput", stage_id=int, plan_id=int, operation_id=str, undo_available=bool),
    "task_create": output("TaskCreateOutput", task_id=int, stage_id=int, operation_id=str, undo_available=bool),
    "submission_create": output("SubmissionCreateOutput", submission_id=int, status=str, task_id=int),
    "submission_get": output("SubmissionGetOutput", id=int, plan_id=int, task_id=int, type=str, status=str),
    "submission_list": output("SubmissionListOutput", submissions=list),
    "submission_check": output("SubmissionCheckOutput", submission_id=int, task_id=int, status=str, task_status=str, score=float, operation_id=str, undo_available=bool),
    "resource_list": output("ResourceListOutput", resources=list),
    "learning_event_list": output("LearningEventListOutput", events=list),
    "study_state_get": output("StudyStateGetOutput", plan_id=int, plan_version=int, progress=float, current_stage=str | None, current_task=dict | None, recommended_next=dict | None, counts=dict, overdue_tasks=list, blocked_tasks=list, scheduled_reviews=list, recent_submissions=list, weekly_minutes=int, completed_estimated_minutes=int, generated_at=str),
    "memory_search": output("MemorySearchOutput", memories=list),
    "memory_maintain": output("MemoryMaintainOutput", expired=int, archived=int, plans_refreshed=int),
    "web_search": output("WebSearchOutput", provider=str, query=str, results=list, saved_resource_ids=list),
    "web_open": output("WebOpenOutput", url=str, title=str, content=str, truncated=bool, redirect_count=int),
    "resource_save": output("ResourceSaveOutput", resource_id=int, plan_id=int, created=bool, operation_id=str, undo_available=bool),
    "file_list": output("FileListOutput", workspace=str, entries=list, truncated=bool),
    "file_read": output("FileReadOutput", path=str, content=str, truncated=bool, size=int),
    "file_write": output("FileWriteOutput", path=str, size=int, operation_id=str, undo_available=bool),
    "code_execute": output("CodeExecuteOutput", exit_code=int, stdout=str, stderr=str, truncated=bool),
    "calendar_list": output("CalendarListOutput", events=list),
    "calendar_create": output("CalendarCreateOutput", event_id=int, starts_at=str, operation_id=str, undo_available=bool),
    "calendar_patch": output("CalendarPatchOutput", event_id=int, status=str, operation_id=str, undo_available=bool),
}


def attach_output_contracts(tools: list[Any]) -> None:
    missing = {tool.name for tool in tools}.difference(TOOL_OUTPUT_MODELS)
    extra = set(TOOL_OUTPUT_MODELS).difference(tool.name for tool in tools)
    if missing or extra:
        raise RuntimeError(f"Tool output contract mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    for tool in tools:
        tool.output_model = TOOL_OUTPUT_MODELS[tool.name]
