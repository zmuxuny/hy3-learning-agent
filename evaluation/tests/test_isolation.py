from __future__ import annotations

import ast
import imaplib
import smtplib
import socket
import sys
import types
import urllib.request
from pathlib import Path
from typing import Any

import pywebpush
from learning_agent_eval.validator import validate_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_SOURCE = PROJECT_ROOT / "evaluation" / "src" / "learning_agent_eval"
MINI_DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "decisionbench-v1"


def test_evaluation_package_has_no_production_or_external_client_imports() -> None:
    prohibited_roots = {
        "aiosqlite",
        "app",
        "httpx",
        "openai",
        "pywebpush",
        "sqlalchemy",
    }
    violations: list[tuple[str, str]] = []
    for path in sorted(EVALUATION_SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.split(".", 1)[0] in prohibited_roots:
                    violations.append((path.name, module))
    assert violations == []


def test_validation_does_not_touch_database_network_or_channels(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[str] = []

    def trap(name: str):
        def fail(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"unexpected external side effect: {name}")

        return fail

    production_database = tmp_path / "production" / "learning_companion.db"
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite+aiosqlite:///{production_database.as_posix()}"
    )
    monkeypatch.setattr(socket, "create_connection", trap("network"))
    monkeypatch.setattr(urllib.request, "urlopen", trap("urlopen"))
    monkeypatch.setattr(smtplib, "SMTP", trap("smtp"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", trap("smtp_ssl"))
    monkeypatch.setattr(imaplib, "IMAP4", trap("imap"))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", trap("imap_ssl"))
    monkeypatch.setattr(pywebpush, "webpush", trap("web_push"))

    fake_outbox = types.ModuleType("app.outbox")
    fake_outbox.dispatch_pending = trap("outbox")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.outbox", fake_outbox)

    report = validate_dataset(MINI_DATASET)

    assert report.ok
    assert calls == []
    assert not production_database.exists()
    assert not production_database.parent.exists()
