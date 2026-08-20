"""Permanent H7 frontend regression gates backed by real Vue/Pinia tests.

The original H0 baselines evaluated fragments of the monolithic workspace
store with handwritten JavaScript stubs. H7 removed that store, so each
defect ID now delegates to the corresponding Vitest suite that mounts the
actual domain stores/components with Vue Router and jsdom.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


def _run_vitest(spec: str, pattern: str) -> None:
    completed = subprocess.run(
        [
            "npm",
            "exec",
            "--",
            "vitest",
            "run",
            f"tests/{spec}",
            "--testNamePattern",
            pattern,
        ],
        cwd=FRONTEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"Vitest contract {spec!r} / {pattern!r} failed.\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


def test_h7_ui_002_live_run_anchors_after_the_latest_steer() -> None:
    _run_vitest(
        "h7-rendering-a11y.spec.js",
        "H7 chronological conversation projection",
    )


def test_h7_ui_003_archiving_a_plan_clears_current_and_focus_scopes() -> None:
    _run_vitest("h7-routing-plan.spec.js", "H7 plan archive state")


def test_h7_ui_004_queued_reminder_reply_carries_its_durable_target() -> None:
    _run_vitest(
        "h7-intervention-optional.spec.js",
        "H7 durable intervention reply target",
    )


def test_h7_ui_005_sse_disconnect_reconciles_with_durable_run_state() -> None:
    _run_vitest(
        "h7-sse-reconciliation.spec.js",
        "H7 SSE reconciliation controller",
    )


def test_h7_ui_006_optional_boot_failure_preserves_core_workspace() -> None:
    _run_vitest(
        "h7-intervention-optional.spec.js",
        "H7 independent core and optional bootstrap",
    )
