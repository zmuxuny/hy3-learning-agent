from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Intervention, Notification, RunApproval, ToolInvocation


EXTERNAL_UNTRUSTED_FIELD = "external_untrusted"
EXTERNAL_CONTENT_TOOL_NAMES = frozenset(
    {"file_list", "file_read", "web_open", "web_search"}
)
WRITE_EFFECT_KINDS = frozenset({"database_write", "external_write"})


@dataclass(frozen=True)
class RunAuthority:
    external_untrusted: bool
    source_run_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    structurally_valid: bool = True
    structural_error: str | None = None


@dataclass(frozen=True)
class ApprovalValidation:
    valid: bool
    reason_code: str
    approval_id: str | None = None
    invocation_id: int | None = None


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    external_untrusted: bool
    requires_approval: bool
    reason_code: str
    source_run_ids: tuple[str, ...] = ()
    approval_id: str | None = None
    invocation_id: int | None = None


def mark_external_untrusted_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a model-visible copy carrying the canonical trust marker."""

    return {**payload, EXTERNAL_UNTRUSTED_FIELD: True}


def is_external_untrusted_result(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get(EXTERNAL_UNTRUSTED_FIELD) is True


async def inspect_run_authority(
    db: AsyncSession,
    run_id: str,
    *,
    owner_id: str | None = None,
) -> RunAuthority:
    """Derive durable authority from a Run and every ancestor.

    Email/read-only identity lives on ``AgentRun``. External-content taint lives
    on a committed ``ToolInvocation`` result so a process restart cannot erase
    it. A child can narrow capabilities but can never regain authority discarded
    by its parent; child-report tools project the same marker back to the parent.
    """

    reasons: list[str] = []
    source_run_ids: list[str] = []
    structural_error: str | None = None
    seen: set[str] = set()
    current_id: str | None = run_id
    while current_id is not None:
        if current_id in seen:
            structural_error = "run_ancestry_cycle"
            source_run_ids.append(current_id)
            break
        seen.add(current_id)
        run = await db.get(AgentRun, current_id)
        if run is None:
            structural_error = "run_missing"
            source_run_ids.append(current_id)
            break
        if owner_id is not None and run.owner_id != owner_id:
            structural_error = "run_owner_mismatch"
            source_run_ids.append(run.id)
            break

        run_reasons: list[str] = []
        if run.trigger == "email_reply":
            run_reasons.append("email_external_source")
        if run.execution_mode == "read_only":
            run_reasons.append("read_only_run")
        external_results = list(
            (
                await db.execute(
                    select(ToolInvocation.result_payload).where(
                        ToolInvocation.run_id == run.id,
                        ToolInvocation.owner_id == run.owner_id,
                        ToolInvocation.status == "committed",
                    )
                )
            ).scalars()
        )
        if any(is_external_untrusted_result(payload) for payload in external_results):
            run_reasons.append("external_read_result")
        if run_reasons:
            source_run_ids.append(run.id)
            reasons.extend(run_reasons)
        current_id = run.parent_run_id

    return RunAuthority(
        external_untrusted=bool(reasons) or structural_error is not None,
        source_run_ids=tuple(dict.fromkeys(source_run_ids)),
        reason_codes=tuple(
            dict.fromkeys([*reasons, *([structural_error] if structural_error else [])])
        ),
        structurally_valid=structural_error is None,
        structural_error=structural_error,
    )


async def validate_read_only_intervention_reply(
    db: AsyncSession,
    *,
    owner_id: str,
    run_id: str,
    tool_name: str,
    canonical_args: dict[str, Any] | None,
) -> bool:
    """Preserve only H5's exact reply capability for an archived email Run."""

    if tool_name != "notification_send" or not isinstance(canonical_args, dict):
        return False
    run = await db.get(AgentRun, run_id)
    if (
        run is None
        or run.owner_id != owner_id
        or run.trigger != "email_reply"
        or run.execution_mode != "read_only"
        or run.reply_to_intervention_id is None
    ):
        return False
    intervention = await db.get(Intervention, run.reply_to_intervention_id)
    if (
        intervention is None
        or intervention.owner_id != owner_id
        or intervention.plan_id != run.plan_id
        or intervention.session_id != run.session_id
        or intervention.state not in {"active", "replied"}
    ):
        return False
    if canonical_args.get("plan_id") not in {None, run.plan_id}:
        return False
    channels = canonical_args.get("channels")
    if not isinstance(channels, list) or not channels:
        return False
    original_channels = set(
        (
            await db.execute(
                select(Notification.channel).where(
                    Notification.intervention_id == intervention.id
                )
            )
        ).scalars()
    )
    return bool(original_channels) and set(channels).issubset(original_channels)


def _approval_arguments_match(approval: RunApproval, invocation: ToolInvocation) -> bool:
    tool_call = approval.tool_call if isinstance(approval.tool_call, dict) else {}
    if tool_call.get("id") != approval.tool_call_id or tool_call.get("name") != approval.tool_name:
        return False
    raw_arguments = tool_call.get("arguments")
    if not isinstance(raw_arguments, str) or not isinstance(invocation.canonical_args, dict):
        return False
    try:
        supplied = json.loads(raw_arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(supplied, dict):
        return False
    # Approval UI and replay both consume the fully-defaulted canonical request
    # projected by ``pause_for_approval``.  A subset is not sufficient: omitted
    # defaults can materially change a write that the user never saw.
    return supplied == invocation.canonical_args


async def project_child_result_authority(
    db: AsyncSession,
    *,
    owner_id: str,
    child_run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Propagate a child's durable external authority into a parent result.

    The caller persists the returned payload in its own ToolInvocation.  This
    makes child-to-parent propagation survive restart without trusting a prompt
    or an in-process flag.
    """

    authority = await inspect_run_authority(db, child_run_id, owner_id=owner_id)
    if not authority.structurally_valid:
        raise ValueError(
            "child authority is structurally invalid: "
            f"{authority.structural_error or 'run_authority_invalid'}"
        )
    if authority.external_untrusted:
        return mark_external_untrusted_result(payload)
    return payload


async def validate_durable_approval(
    db: AsyncSession,
    *,
    owner_id: str,
    run_id: str,
    tool_call_id: str | None,
    tool_name: str,
    request_digest: str | None,
    invocation_id: int | None = None,
    claim_token: str | None = None,
    claim_version: int | None = None,
) -> ApprovalValidation:
    """Validate an approval against its exact durable invocation/request."""

    if not tool_call_id or not request_digest:
        return ApprovalValidation(False, "approval_identity_missing")
    approval = (
        await db.execute(
            select(RunApproval).where(
                RunApproval.owner_id == owner_id,
                RunApproval.run_id == run_id,
                RunApproval.tool_call_id == tool_call_id,
            )
        )
    ).scalars().one_or_none()
    if approval is None:
        return ApprovalValidation(False, "approval_missing")
    if approval.decision != "approve" or approval.decided_at is None:
        return ApprovalValidation(False, "approval_not_approved", approval.id)
    if approval.consumed_at is None:
        return ApprovalValidation(False, "approval_not_consumed", approval.id)
    if approval.tool_name != tool_name or approval.invocation_id is None:
        return ApprovalValidation(False, "approval_tool_mismatch", approval.id)
    if invocation_id is not None and approval.invocation_id != invocation_id:
        return ApprovalValidation(False, "approval_invocation_mismatch", approval.id)

    invocation = await db.get(ToolInvocation, approval.invocation_id)
    if invocation is None:
        return ApprovalValidation(False, "approval_invocation_missing", approval.id)
    common = ApprovalValidation(
        False,
        "approval_invocation_mismatch",
        approval.id,
        invocation.id,
    )
    if (
        invocation.owner_id != owner_id
        or invocation.run_id != run_id
        or invocation.tool_call_id != tool_call_id
        or invocation.tool_name != tool_name
        or invocation.effect_kind not in WRITE_EFFECT_KINDS
    ):
        return common
    if invocation.request_digest != request_digest or invocation.args_hash != request_digest:
        return ApprovalValidation(
            False,
            "approval_request_mismatch",
            approval.id,
            invocation.id,
        )
    if invocation.status not in {"pending_approval", "running"}:
        return ApprovalValidation(
            False,
            "approval_invocation_state",
            approval.id,
            invocation.id,
        )
    if invocation.status == "running":
        if (
            not claim_token
            or claim_version is None
            or invocation.claim_token != claim_token
            or invocation.version != claim_version
        ):
            return ApprovalValidation(
                False,
                "approval_claim_mismatch",
                approval.id,
                invocation.id,
            )
    if not _approval_arguments_match(approval, invocation):
        return ApprovalValidation(
            False,
            "approval_projection_mismatch",
            approval.id,
            invocation.id,
        )
    return ApprovalValidation(True, "approved_exact_request", approval.id, invocation.id)


async def authorize_side_effect(
    db: AsyncSession,
    *,
    owner_id: str,
    run_id: str,
    tool_call_id: str | None,
    tool_name: str,
    effect_kind: str,
    request_digest: str | None,
    invocation_id: int | None = None,
    claim_token: str | None = None,
    claim_version: int | None = None,
    canonical_args: dict[str, Any] | None = None,
) -> AuthorityDecision:
    """Fail closed when an untrusted Run has no exact durable approval."""

    if effect_kind not in WRITE_EFFECT_KINDS:
        return AuthorityDecision(True, False, False, "read_effect")
    authority = await inspect_run_authority(db, run_id, owner_id=owner_id)
    if not authority.structurally_valid:
        return AuthorityDecision(
            False,
            True,
            False,
            authority.structural_error or "run_authority_invalid",
            authority.source_run_ids,
        )
    if not authority.external_untrusted:
        return AuthorityDecision(
            True,
            False,
            False,
            "trusted_run",
            authority.source_run_ids,
        )
    if await validate_read_only_intervention_reply(
        db,
        owner_id=owner_id,
        run_id=run_id,
        tool_name=tool_name,
        canonical_args=canonical_args,
    ):
        return AuthorityDecision(
            True,
            True,
            False,
            "validated_read_only_intervention_reply",
            authority.source_run_ids,
        )
    approval = await validate_durable_approval(
        db,
        owner_id=owner_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        request_digest=request_digest,
        invocation_id=invocation_id,
        claim_token=claim_token,
        claim_version=claim_version,
    )
    return AuthorityDecision(
        allowed=approval.valid,
        external_untrusted=True,
        requires_approval=not approval.valid,
        reason_code=approval.reason_code,
        source_run_ids=authority.source_run_ids,
        approval_id=approval.approval_id,
        invocation_id=approval.invocation_id,
    )
