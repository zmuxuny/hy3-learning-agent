"""Fail-closed process guards and minimal worker environment construction."""

from __future__ import annotations

import os
import socket
import sys
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .runtime_metadata import HY3_API_BASE, HY3_MODEL


class EvaluationIsolationError(RuntimeError):
    pass


def _network_host(value: Any) -> str:
    # AnyIO passes IDNA-encoded bytes to socket.getaddrinfo. str(bytes)
    # includes the b'...' wrapper and incorrectly rejects the allowed host.
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return ""
    return str(value).casefold()


def worker_environment(
    *,
    project_root: Path,
    worker_root: Path,
    model_mode: str,
    allow_real_model: bool,
) -> dict[str, str]:
    """Return a whitelist environment; never clone the parent process."""

    database_path = (worker_root / "data" / "learning_companion.db").resolve()
    environment = {
        "PYTHONPATH": os.pathsep.join(
            [str(project_root / "backend"), str(project_root / "evaluation" / "src")]
        ),
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "EVALUATION_MODE": "1",
        "RUNTIME_STATE_ROOT": str(worker_root.resolve()),
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
        "ENABLE_SCHEDULER": "false",
        "ENABLE_EMAIL_REPLY_POLLING": "false",
        "OPENAI_API_KEY": "evaluation-stub-placeholder",
        "OPENAI_API_BASE": "https://model.example.invalid/v1",
        "MODEL_NAME": "e1-scripted-model",
        "MODEL_TEMPERATURE": "0",
        "MODEL_REASONING_EFFORT": "none",
        "AGENT_RUN_MAX_RETRIES": "0",
        "AGENT_MAX_STEPS": "4",
        "AGENT_MAX_MODEL_CALLS": "4",
        "AGENT_MAX_TOOL_CALLS": "4",
        "SMTP_HOST": "smtp.example.invalid",
        "SMTP_USERNAME": "evaluation-sender",
        "SMTP_PASSWORD": "evaluation-placeholder",
        "SMTP_FROM": "sender@example.test",
        "SMTP_TO": "recipient@example.test",
        "SMTP_USE_TLS": "false",
        "SMTP_USE_SSL": "false",
        "VAPID_PUBLIC_KEY": "evaluation-public-placeholder",
        "VAPID_PRIVATE_KEY": "evaluation-private-placeholder",
        "VAPID_SUBJECT": "mailto:push@example.test",
        "CODE_SANDBOX_PROVIDER": "none",
    }
    if model_mode == "real":
        if not allow_real_model:
            raise EvaluationIsolationError("real model requires explicit opt-in")
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise EvaluationIsolationError("real model requires caller OPENAI_API_KEY")
        environment["OPENAI_API_KEY"] = key
        environment["OPENAI_API_BASE"] = os.environ.get(
            "OPENAI_API_BASE", HY3_API_BASE
        )
        environment["MODEL_NAME"] = os.environ.get("MODEL_NAME", HY3_MODEL)
        environment["MODEL_TEMPERATURE"] = "0.9"
        environment["MODEL_REASONING_EFFORT"] = "high"
        environment["AGENT_MAX_STEPS"] = "8"
        environment["AGENT_MAX_MODEL_CALLS"] = "8"
        environment["AGENT_MAX_TOOL_CALLS"] = "16"
    model_url = urlsplit(environment["OPENAI_API_BASE"])
    if (
        model_url.scheme != "https"
        or not model_url.hostname
        or model_url.username
        or model_url.password
        or model_url.query
        or model_url.fragment
    ):
        raise EvaluationIsolationError("model provider URL must be credential-free HTTPS")
    if model_mode == "real" and (
        environment["OPENAI_API_BASE"].rstrip("/") != HY3_API_BASE
        or environment["MODEL_NAME"] != HY3_MODEL
    ):
        raise EvaluationIsolationError(
            "real model endpoint and model must match the Hy3 allowlist"
        )
    return environment


class IsolationGuard:
    """Process-local traps installed before any production application import."""

    def __init__(
        self,
        *,
        project_root: Path,
        worker_root: Path,
        model_mode: str,
        model_base_url: str,
    ):
        self.project_root = project_root.resolve()
        self.worker_root = worker_root.resolve()
        self.model_mode = model_mode
        self.model_host = (urlsplit(model_base_url).hostname or "").casefold()
        self.allowed_addresses: set[str] = set()
        self.counters = {
            "network_calls": 0,
            "smtp_calls": 0,
            "smtp_ssl_calls": 0,
            "web_push_calls": 0,
            "imap_calls": 0,
            "imap_ssl_calls": 0,
            "prohibited_file_access": 0,
            "outside_sqlite_access": 0,
            "subprocess_calls": 0,
        }

    def install(self) -> None:
        sys.addaudithook(self._audit)
        self._install_network_resolution_guard()
        self._install_provider_traps()

    def _audit(self, event: str, arguments: tuple[Any, ...]) -> None:
        if event == "open" and arguments:
            raw = arguments[0]
            if isinstance(raw, (str, bytes, os.PathLike)):
                supplied = Path(os.fsdecode(raw)).expanduser()
                if not supplied.is_absolute():
                    supplied = Path.cwd() / supplied
                path = supplied.resolve()
                protected_data = self.project_root / "data"
                if path == self.project_root / ".env" or (
                    path == protected_data or protected_data in path.parents
                ):
                    self.counters["prohibited_file_access"] += 1
                    raise EvaluationIsolationError("repository runtime data access denied")
        elif event == "sqlite3.connect" and arguments:
            raw = str(arguments[0])
            if raw != ":memory:":
                if raw.startswith("file:"):
                    raw = unquote(raw[5:].split("?", 1)[0])
                path = Path(raw).expanduser().resolve()
                if path != self.worker_root and self.worker_root not in path.parents:
                    self.counters["outside_sqlite_access"] += 1
                    raise EvaluationIsolationError("SQLite target outside worker root")
        elif event == "subprocess.Popen":
            self.counters["subprocess_calls"] += 1
            raise EvaluationIsolationError("worker subprocess execution denied")
        elif event == "socket.connect" and arguments:
            address = arguments[1] if len(arguments) > 1 else None
            host = _network_host(address[0]) if isinstance(address, tuple) and address else ""
            port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
            if self.model_mode != "real" or port != 443 or host not in self.allowed_addresses | {self.model_host}:
                self.counters["network_calls"] += 1
                raise EvaluationIsolationError("network target denied")

    def _install_network_resolution_guard(self) -> None:
        original = socket.getaddrinfo

        def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
            normalized = _network_host(host)
            if self.model_mode != "real" or normalized != self.model_host:
                self.counters["network_calls"] += 1
                raise EvaluationIsolationError("DNS target denied")
            result = original(host, *args, **kwargs)
            for item in result:
                if item and len(item) >= 5 and item[4]:
                    self.allowed_addresses.add(str(item[4][0]).casefold())
            return result

        socket.getaddrinfo = guarded

    def _trap(self, counter: str):
        def fail(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.counters[counter] += 1
            raise EvaluationIsolationError(f"external provider denied: {counter}")

        return fail

    def _install_provider_traps(self) -> None:
        smtplib = import_module("smtplib")
        imaplib = import_module("imaplib")
        pywebpush = import_module("pywebpush")
        smtplib.SMTP = self._trap("smtp_calls")
        smtplib.SMTP_SSL = self._trap("smtp_ssl_calls")
        imaplib.IMAP4 = self._trap("imap_calls")
        imaplib.IMAP4_SSL = self._trap("imap_ssl_calls")
        pywebpush.webpush = self._trap("web_push_calls")
        pywebpush.WebPusher = self._trap("web_push_calls")

    def evidence(self) -> dict[str, Any]:
        return {
            **self.counters,
            "env_file_read": False,
            "database_inside_worker_root": self.counters["outside_sqlite_access"] == 0,
            "repository_runtime_data_access": False,
            "background_services_started": False,
        }
