"""Budget enforcement and preparation of non-formal real-mode pilot inputs."""

import asyncio
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from learning_agent_eval.case_authoring import write_candidate_suite
from learning_agent_eval.isolation import EvaluationIsolationError, IsolationGuard
from learning_agent_eval.model_budget import (
    RESERVATION,
    ModelBudget,
    ModelBudgetExceeded,
)
from learning_agent_eval.models import BenchmarkReleaseManifestV1
from learning_agent_eval.recorder import EvaluationModelRecorder, RecorderBudgetExceeded
from learning_agent_eval.validator import validate_dataset
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


def test_pilot_preparation_is_valid_and_cannot_be_released(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "pilot_builder", ROOT / "evaluation/scripts/prepare_protocol_pilot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = module.pilot_cases()
    assert len(cases) == 4
    assert len({case["track"] for case in cases}) == 4
    assert all(
        case["dataset_role"] == "protocol_pilot"
        and not case["runtime_setup"]["scripted_turns"]
        for case in cases
    )
    assert all(
        case["private_annotations"]["reviewer_role"] == "pending_human_review"
        for case in cases
    )
    output = write_candidate_suite(
        tmp_path / "suite",
        cases=cases,
        resource_snapshot=ROOT
        / "evaluation/datasets/decisionbench-v4-engineering/resources/snapshot.json",
        dataset_version="test-protocol-pilot",
    )
    assert validate_dataset(output).ok
    import json

    release = json.loads((output / "benchmark-release.json").read_text())
    release["release_status"] = "released"
    with pytest.raises(ValidationError, match="pilots cannot become"):
        BenchmarkReleaseManifestV1.model_validate(release)
    invalid = deepcopy(cases)
    invalid.append(deepcopy(cases[0]))
    with pytest.raises(ValueError):
        write_candidate_suite(
            tmp_path / "invalid",
            cases=invalid,
            resource_snapshot=ROOT
            / "evaluation/datasets/decisionbench-v4-engineering/resources/snapshot.json",
            dataset_version="duplicate-pilot",
        )
    assert not (tmp_path / "invalid").exists()


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_prepaid_request_budget():
    requests = []
    overlapping = 0
    peak = 0

    async def create(**request):
        nonlocal overlapping, peak
        requests.append(request)
        overlapping += 1
        peak = max(peak, overlapping)
        await asyncio.sleep(0.01)
        overlapping -= 1
        return SimpleNamespace(
            model="stub",
            id="response",
            usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=20, total_tokens=120
            ),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="public", tool_calls=[])
                )
            ],
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    recorder = EvaluationModelRecorder(
        client, invocation_mode="stub", max_calls=3, max_output_tokens=16000
    )
    results = await asyncio.gather(
        *(
            recorder.chat.completions.create(
                model="stub", messages=[], n=9, max_completion_tokens=100000
            )
            for _ in range(12)
        ),
        return_exceptions=True,
    )
    assert len(requests) == peak == 3
    assert sum(isinstance(result, RecorderBudgetExceeded) for result in results) == 9
    assert all(
        request["n"] == 1
        and request["max_tokens"] == 16000
        and "max_completion_tokens" not in request
        for request in requests
    )
    assert [record["ordinal"] for record in recorder.records] == [1, 2, 3]
    assert all(
        record["token_usage"]["total_tokens"] == 120 for record in recorder.records
    )
    with pytest.raises(RecorderBudgetExceeded, match="override"):
        await recorder.chat.completions.create(model="stub", messages=[], extra_body={"n": 100})
    assert len(requests) == 3


def test_shared_money_ledger_reserves_before_calls_and_never_refunds_unknown_usage(tmp_path):
    path = tmp_path / "budget.json"
    budget = ModelBudget.create(path, limit_micro_cny=3 * RESERVATION)

    def attempt(index):
        try:
            return ModelBudget(path).reserve(scope=f"worker-{index}", call_id="call-1")
        except ModelBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        tickets = list(executor.map(attempt, range(12)))
    assert sorted(ticket for ticket in tickets if ticket is not None) == [1, 2, 3]
    budget.settle(1, None)
    budget.settle(2, {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
    assert budget.summary()["charged_micro_cny"] == 2 * RESERVATION + 300
    with pytest.raises(ModelBudgetExceeded):
        budget.reserve(scope="extra", call_id="call-1")
    with pytest.raises(ValueError, match="already"):
        budget.settle(2, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    budget.settle(3, {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 100})
    assert budget.summary()["blocked"]
    assert budget.summary()["charged_micro_cny"] == 2 * RESERVATION + 300
    with pytest.raises(FileExistsError):
        ModelBudget.create(path, limit_micro_cny=14_000_000)


def test_real_dns_accepts_anyio_bytes_but_denies_other_hosts_and_ports(tmp_path, monkeypatch):
    import socket

    seen = []

    def resolve(host, *args, **kwargs):
        seen.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    guard = IsolationGuard(project_root=tmp_path / "repo", worker_root=tmp_path / "worker",
                           model_mode="real", model_base_url="https://tokenhub.tencentmaas.com/v1")
    guard._install_network_resolution_guard()
    socket.getaddrinfo(b"tokenhub.tencentmaas.com", 443)
    assert seen == [b"tokenhub.tencentmaas.com"]
    guard._audit("socket.connect", (None, ("192.0.2.8", 443)))
    assert guard.counters["network_calls"] == 0
    with pytest.raises(EvaluationIsolationError):
        socket.getaddrinfo(b"other.example.test", 443)
    with pytest.raises(EvaluationIsolationError):
        guard._audit("socket.connect", (None, ("192.0.2.8", 25)))
    assert len(seen) == 1


def test_inspection_does_not_exempt_writes_or_final_answers():
    from learning_agent_eval.action_protocol import inspection_only_response

    assert inspection_only_response("", [{"name":"plan_get"}, {"name":"submission_get"}])
    assert not inspection_only_response("All accepted", [{"name":"plan_get"}])
    assert not inspection_only_response("", [{"name":"plan_get"}, {"name":"plan_patch"}])
    assert not inspection_only_response("", [{"name":"unknown_read_tool"}])
    assert not inspection_only_response("", [])
