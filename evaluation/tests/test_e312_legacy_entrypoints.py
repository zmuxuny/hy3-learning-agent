from __future__ import annotations

import imaplib
import json
import smtplib
import socket
import sqlite3
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import pywebpush
from learning_agent_eval import __all__ as public_api
from learning_agent_eval import (
    aggregate,
    aggregate_v2,
    judge,
    judge_v2,
    rule_runner,
    rule_runner_v2,
    runner,
    runner_v3,
    worker,
    worker_v3,
)
from learning_agent_eval.cli import main as cli_main
from learning_agent_eval.legacy import LegacyExecutionDisabledError


def _manifest(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
    return path


def _install_external_traps(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counters = {
        "network": 0,
        "smtp": 0,
        "smtp_ssl": 0,
        "web_push": 0,
        "imap": 0,
        "imap_ssl": 0,
        "database": 0,
        "subprocess": 0,
    }

    def trap(name: str):
        def blocked(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            counters[name] += 1
            raise AssertionError(f"legacy entry attempted {name}")

        return blocked

    monkeypatch.setattr(socket, "create_connection", trap("network"))
    monkeypatch.setattr(urllib.request, "urlopen", trap("network"))
    monkeypatch.setattr(smtplib, "SMTP", trap("smtp"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", trap("smtp_ssl"))
    monkeypatch.setattr(pywebpush, "webpush", trap("web_push"))
    monkeypatch.setattr(imaplib, "IMAP4", trap("imap"))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", trap("imap_ssl"))
    monkeypatch.setattr(sqlite3, "connect", trap("database"))
    monkeypatch.setattr(subprocess, "run", trap("subprocess"))
    monkeypatch.setattr(subprocess, "Popen", trap("subprocess"))
    return counters


def test_package_public_api_exposes_only_active_execution_chain() -> None:
    assert {
        "run_active_runtime",
        "evaluate_active_rules",
        "evaluate_active_judges",
        "aggregate_active_results",
    } <= set(public_api)
    assert not {
        "run_agent",
        "run_agent_v3",
        "run_agent_v4",
        "evaluate_run_rules",
        "evaluate_run_rules_v2",
        "evaluate_run_rules_v3",
        "evaluate_judges",
        "evaluate_judges_v2",
        "evaluate_judges_v3",
        "aggregate_results",
        "aggregate_results_v2",
        "aggregate_results_v3",
    }.intersection(public_api)


def test_all_historical_python_executors_fail_before_argument_access() -> None:
    entries = (
        runner.run_agent,
        runner_v3.run_agent_v3,
        runner_v3.run_agent_v4,
        rule_runner.evaluate_run_rules,
        rule_runner_v2.evaluate_run_rules_v2,
        rule_runner_v2.evaluate_run_rules_v3,
        judge.evaluate_judges,
        judge_v2.evaluate_judges_v2,
        judge_v2.evaluate_judges_v3,
        aggregate.aggregate_results,
        aggregate_v2.aggregate_results_v2,
        aggregate_v2.aggregate_results_v3,
    )
    poison = object()
    for entry in entries:
        with pytest.raises(LegacyExecutionDisabledError) as caught:
            entry(poison, output=poison)
        assert caught.value.code == "legacy_execution_disabled"
        assert caught.value.as_dict()["stage"] == "preflight"


def test_historical_workers_return_structured_disabled_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for entry in (worker.main, worker_v3.main):
        assert entry() == 1
        error = json.loads(capsys.readouterr().err)
        assert error["error_code"] == "legacy_execution_disabled"
        assert error["stage"] == "preflight"


def test_historical_cli_inputs_have_zero_external_or_output_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    counters = _install_external_traps(monkeypatch)
    old_suite = _manifest(tmp_path / "dataset" / "manifest.json", "case-suite-manifest-v1")
    old_runtime = _manifest(
        tmp_path / "old-runtime" / "run-manifest.json", "runtime-run-manifest-v2"
    )
    old_rules = _manifest(
        tmp_path / "old-rules" / "rule-manifest.json", "rule-run-manifest-v2"
    )
    old_judges = _manifest(
        tmp_path / "old-judges" / "run-manifest.json", "judge-run-manifest-v2"
    )
    outputs = [tmp_path / f"forbidden-output-{index}" for index in range(4)]
    commands = (
        [
            "run-agent",
            "--dataset",
            str(old_suite.parent),
            "--manifest",
            str(old_suite),
            "--output",
            str(outputs[0]),
            "--model-mode",
            "real",
            "--allow-real-model",
        ],
        [
            "evaluate-rules",
            "--input",
            str(old_runtime.parent),
            "--output",
            str(outputs[1]),
        ],
        [
            "evaluate-judge",
            "--episodes",
            str(old_runtime.parent),
            "--rules",
            str(old_rules.parent),
            "--output",
            str(outputs[2]),
            "--judge-mode",
            "real",
            "--allow-real-judge",
        ],
        [
            "aggregate-results",
            "--episodes",
            str(old_runtime.parent),
            "--rules",
            str(old_rules.parent),
            "--judges",
            str(old_judges.parent),
            "--output",
            str(outputs[3]),
        ],
    )
    for command in commands:
        assert cli_main(command) == 1
        error = json.loads(capsys.readouterr().err)
        assert error["error_code"] == "legacy_execution_disabled"
        assert error["stage"] == "preflight"
    assert counters == {name: 0 for name in counters}
    assert all(not path.exists() for path in outputs)
    assert not list(tmp_path.rglob("captures"))
