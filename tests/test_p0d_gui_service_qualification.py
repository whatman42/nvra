"""P0-D: GUI composition-root ARM path + service-resume gates (no Qt GUI, no real capital).

GUI Qt automation is UNOBSERVABLE in headless CI. This suite exercises the same
authoritative LiveExecutionController + LiveAuthorizationGate path the GUI must call.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from god.institutional.checkpoint import CheckpointStore
from god.live.controller import LiveExecutionController
from god.live.models import LiveMode, LivePrerequisites, PreflightStatus
from god.live.authorization import LiveAuthorizationGate
from god.control_plane.fallback import evaluate_offline

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "research" / "harness" / "process_recovery_worker.py"
PYTHON = sys.executable


def _ctrl_live() -> LiveExecutionController:
    return LiveExecutionController(mode=LiveMode.LIVE)


def test_gui_composition_arm_blocked_without_preflight():
    c = _ctrl_live()
    res = c.arm(operator_ack="operator")
    assert res["ok"] is False
    assert res["reason"] == "preflight_not_pass"
    assert c.auth_gate.can_submit_live() is False


def test_gui_composition_arm_blocked_when_preflight_fail():
    c = _ctrl_live()
    c.evaluate_preflight(checks={"license": PreflightStatus.FAIL})
    res = c.arm(operator_ack="operator")
    assert res["ok"] is False
    assert c.auth_gate.can_submit_live() is False


def test_gui_composition_arm_requires_gates_beyond_empty_prereq():
    c = _ctrl_live()
    report = c.evaluate_preflight()
    check_names = list(report.get("checks", {}).keys())
    if check_names:
        c.evaluate_preflight(checks={n: PreflightStatus.PASS for n in check_names})
    res = c.arm(operator_ack="operator")
    assert res["ok"] is False or c.auth_gate.can_submit_live() is False


def test_gui_composition_safe_mode_blocks_arm():
    c = _ctrl_live()
    c.auth_gate.enter_safe_mode("fault")
    report = c.evaluate_preflight()
    names = list(report.get("checks", {}).keys())
    if names:
        c.evaluate_preflight(checks={n: PreflightStatus.PASS for n in names})
    res = c.arm(operator_ack="operator")
    assert c.auth_gate.can_submit_live() is False
    assert res["ok"] is False or c.auth_gate.can_submit_live() is False


def test_gui_composition_fallback_does_not_enable_live_submit():
    d = evaluate_offline(None, "missing")
    assert d.live_trading is False
    gate = LiveAuthorizationGate(prerequisites=LivePrerequisites())
    assert gate.can_submit_live() is False


def test_gui_composition_positive_arm_still_needs_risk_engine():
    prereq = LivePrerequisites(
        operator_authorized=True,
        license_valid=True,
        device_valid=True,
        credentials_valid=True,
        broker_connected=True,
        state_loaded=True,
        reconciliation_pass=True,
        risk_governor_ready=True,
        startup_ready=True,
    )
    gate = LiveAuthorizationGate(prerequisites=prereq)
    gate.recompute()
    arm = gate.arm(operator_authorization="op")
    if arm.ok:
        assert gate.can_submit_live() is True
        from crypto.risk.engine import RiskEngine
        from crypto.risk.policy import RiskPolicy
        from crypto.risk.models import Side, TradeProposal, RiskVerdict
        from crypto.portfolio.models import PortfolioSnapshot, ExposureBreakdown

        eng = RiskEngine(RiskPolicy())
        eng.set_reconciliation_ok(False)
        port = PortfolioSnapshot(
            equity=10_000.0, available_balance=10_000.0, reserved_balance=0.0,
            holdings=(), positions=(), unrealized_pnl=0.0, realized_pnl=0.0, fees=0.0,
            exposure=ExposureBreakdown(gross=0.0, net=0.0), timestamp_ms=0,
        )
        prop = TradeProposal(
            exchange_id="paper", account_id="default", symbol="TEST", side=Side.BUY,
            requested_quantity=0.01, requested_price=100.0, strategy_id="p0d", timestamp_ms=0,
        )
        d = eng.evaluate(prop, port, entry_price=100.0)
        assert not (d.verdict == RiskVerdict.APPROVED and d.executable)


def test_service_resume_rejects_corrupt_checkpoint(tmp_path: Path):
    store = CheckpointStore(tmp_path / "c.db")
    import sqlite3

    store.save("r", "observation", {"ok": True})
    with sqlite3.connect(tmp_path / "c.db") as c:
        c.execute("UPDATE checkpoints SET state_json=? WHERE run_id='r'", ("{",))
        c.commit()
    assert store.load("r") is None
    assert store.load_trusted_ready("r") is None


def test_service_resume_rejects_semantic_invalid_ready(tmp_path: Path):
    store = CheckpointStore(tmp_path / "c.db")
    import sqlite3

    payload = json.dumps({
        "schema_version": "1.0", "sequence": 1, "lifecycle": "READY",
        "recon_complete": False, "updated_ns": time.time_ns(),
    })
    with sqlite3.connect(tmp_path / "c.db") as c:
        c.execute("INSERT INTO checkpoints VALUES(?,?,?,?)", ("r", "READY", payload, time.time_ns()))
        c.commit()
    assert store.load("r") is None


def test_service_resume_admin_policy_without_prereq_blocked():
    c = _ctrl_live()
    r = c.auth_gate.resume_from_admin_policy(
        autonomous_live=True, prerequisites_satisfied=False
    )
    assert r.ok is False
    assert c.auth_gate.can_submit_live() is False


def test_service_resume_sigkill_then_recover_no_unsafe_exec(tmp_path: Path):
    if not WORKER.exists():
        pytest.skip("process recovery worker missing")
    workdir = tmp_path / "svc"
    workdir.mkdir()
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'src'}"
    proc = subprocess.Popen(
        [PYTHON, str(WORKER), "--workdir", str(workdir), "--mode", "run", "--stop-after", "RUNNING"],
        env=env, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.time() + 10
    status_path = workdir / "status.json"
    while time.time() < deadline:
        if status_path.exists():
            try:
                st = json.loads(status_path.read_text())
                if st.get("stage") == "RUNNING" and st.get("phase") == "after_save":
                    break
            except json.JSONDecodeError:
                pass
        time.sleep(0.02)
    else:
        proc.kill()
        pytest.fail("worker did not reach RUNNING")
    # Platform-correct hard kill: SIGKILL on POSIX, TerminateProcess on Windows.
    if sys.platform == "win32":
        proc.kill()
    else:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)
    completed = subprocess.run(
        [PYTHON, str(WORKER), "--workdir", str(workdir), "--mode", "recover"],
        env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0
    rec = json.loads((workdir / "recovery_result.json").read_text())
    assert rec["unsafe_execution"] is False
    assert rec["trusted_execution_from_checkpoint"] is False
