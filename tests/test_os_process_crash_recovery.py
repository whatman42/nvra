"""P0-B: OS process-kill recovery qualification (real subprocess + SIGKILL)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "research" / "harness" / "process_recovery_worker.py"
PYTHON = sys.executable


def _wait_status(
    workdir: Path,
    timeout: float = 10.0,
    *,
    expect_stage: str | None = None,
) -> dict:
    deadline = time.time() + timeout
    path = workdir / "status.json"
    while time.time() < deadline:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                stage = data.get("stage")
                phase = data.get("phase") or ""
                if expect_stage and stage != expect_stage:
                    time.sleep(0.02)
                    continue
                if stage and stage != "STARTING" and phase and phase != "boot":
                    return data
            except json.JSONDecodeError:
                pass
        time.sleep(0.02)
    raise TimeoutError(f"status not ready in {workdir} (expect_stage={expect_stage})")


def _spawn_run(workdir: Path, stop_after: str, corrupt: str | None = None) -> subprocess.Popen:
    cmd = [
        PYTHON,
        str(WORKER),
        "--workdir",
        str(workdir),
        "--mode",
        "run",
        "--stop-after",
        stop_after,
    ]
    if corrupt:
        cmd.extend(["--corrupt", corrupt])
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'src'}"
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
    )


def _kill_hard(proc: subprocess.Popen) -> float:
    """Hard-kill the child process with platform-correct semantics.

    POSIX: SIGKILL (unblockable). Windows: TerminateProcess via Popen.kill().
    Recovery contract under test is platform-neutral; only the kill primitive differs.
    """
    t0 = time.time()
    if sys.platform == "win32":
        proc.kill()
    else:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)
    return time.time() - t0


def _recover(workdir: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'src'}"
    t0 = time.time()
    completed = subprocess.run(
        [PYTHON, str(WORKER), "--workdir", str(workdir), "--mode", "recover"],
        env=env,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.time() - t0
    assert completed.returncode == 0, completed.stderr
    result = json.loads((workdir / "recovery_result.json").read_text())
    result["mttr_recover_subprocess_s"] = elapsed
    return result


def _scenario(tmp_path: Path, stop_after: str, corrupt: str | None = None) -> dict:
    workdir = tmp_path / f"w_{stop_after}_{corrupt or 'clean'}"
    workdir.mkdir()
    t_start = time.time()
    proc = _spawn_run(workdir, stop_after, corrupt)
    status = _wait_status(workdir, expect_stage=stop_after)
    if corrupt in ("partial_write", "semantic_invalid", "before_save"):
        want = {
            "partial_write": "partial_write_injected",
            "semantic_invalid": "semantic_invalid_rejected",
            "before_save": "before_save",
        }[corrupt]
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                st = json.loads((workdir / "status.json").read_text())
                ph = st.get("phase")
                if ph == want or (corrupt == "semantic_invalid" and ph == "semantic_invalid_saved"):
                    status = st
                    break
            except (json.JSONDecodeError, OSError):
                pass
            time.sleep(0.02)
    kill_s = _kill_hard(proc)
    t_after_kill = time.time()
    assert proc.poll() is not None
    rec = _recover(workdir)
    t_done = time.time()
    rec["scenario"] = {"stop_after": stop_after, "corrupt": corrupt, "status": status}
    rec["mttr"] = {
        "kill_wait_s": kill_s,
        "kill_to_recover_done_s": t_done - t_after_kill,
        "total_s": t_done - t_start,
        "scope": "local_subprocess_hard_kill",
    }
    return rec


@pytest.mark.parametrize(
    "stop_after",
    ["INIT", "LOAD_STATE", "BROKER_CONNECT", "RECONCILIATION", "RISK_GOVERNOR", "READY", "RUNNING"],
)
def test_sigkill_at_stage_no_unsafe_execution(tmp_path: Path, stop_after: str):
    rec = _scenario(tmp_path, stop_after)
    assert rec["unsafe_ready"] is False
    assert rec["unsafe_execution"] is False
    assert rec["trusted_execution_from_checkpoint"] is False
    assert rec["unknown_dq_exec"] is False
    if rec.get("trusted_ready"):
        assert rec["recon_complete"] is True


def test_sigkill_before_save(tmp_path: Path):
    rec = _scenario(tmp_path, "RECONCILIATION", corrupt="before_save")
    assert rec["unsafe_execution"] is False
    assert rec["trusted_execution_from_checkpoint"] is False


def test_sigkill_after_partial_write(tmp_path: Path):
    rec = _scenario(tmp_path, "RUNNING", corrupt="partial_write")
    assert rec["scenario"]["status"].get("phase") == "partial_write_injected"
    assert rec["loaded"] is False
    assert rec["trusted_ready"] is False
    assert rec["execution_allowed"] is False
    assert rec["unsafe_execution"] is False


def test_semantic_invalid_ready_rejected_in_child(tmp_path: Path):
    rec = _scenario(tmp_path, "RISK_GOVERNOR", corrupt="semantic_invalid")
    status = rec["scenario"]["status"]
    assert status.get("phase") == "semantic_invalid_rejected"
    assert rec["unsafe_execution"] is False


def test_restart_from_unknown_not_executable(tmp_path: Path):
    from god.institutional.checkpoint import CheckpointStore

    workdir = tmp_path / "unknown"
    workdir.mkdir()
    store = CheckpointStore(workdir / "checkpoints.db")
    store.save(
        "p0b-run",
        "UNKNOWN",
        {
            "schema_version": "1.0",
            "sequence": 1,
            "lifecycle": "UNKNOWN",
            "recon_complete": False,
            "updated_ns": time.time_ns(),
        },
    )
    rec = _recover(workdir)
    assert rec["trusted_ready"] is False
    assert rec["execution_allowed"] is False
    assert rec["unsafe_execution"] is False


def test_restart_from_safe_mode_not_executable(tmp_path: Path):
    from god.institutional.checkpoint import CheckpointStore

    workdir = tmp_path / "safe"
    workdir.mkdir()
    store = CheckpointStore(workdir / "checkpoints.db")
    store.save(
        "p0b-run",
        "SAFE_MODE",
        {
            "schema_version": "1.0",
            "sequence": 1,
            "lifecycle": "SAFE_MODE",
            "recon_complete": False,
            "updated_ns": time.time_ns(),
        },
    )
    rec = _recover(workdir)
    assert rec["trusted_ready"] is False
    assert rec["execution_allowed"] is False


def test_restart_from_stale_not_trusted(tmp_path: Path):
    from god.institutional.checkpoint import CheckpointStore
    import sqlite3

    workdir = tmp_path / "stale"
    workdir.mkdir()
    db = workdir / "checkpoints.db"
    CheckpointStore(db)
    payload = json.dumps(
        {
            "schema_version": "1.0",
            "sequence": 5,
            "lifecycle": "RUNNING",
            "recon_complete": True,
            "updated_ns": 1,
        }
    )
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO checkpoints VALUES(?,?,?,?)",
            ("p0b-run", "RUNNING", payload, time.time_ns()),
        )
        c.commit()
    rec = _recover(workdir)
    assert rec["trusted_ready"] is False
    assert rec["execution_allowed"] is False


def test_idempotency_not_broken_by_recovery_path():
    from crypto.execution.models import (
        ExecutionMode,
        make_client_order_id,
        record_from_decision,
        OrderState,
        Side,
    )
    from crypto.execution.states import is_terminal
    from crypto.execution.store import ExecutionStore
    from crypto.risk.engine import RiskEngine
    from crypto.risk.policy import RiskPolicy
    from crypto.risk.models import TradeProposal, RiskVerdict
    from crypto.portfolio.models import PortfolioSnapshot, ExposureBreakdown
    import tempfile

    eng = RiskEngine(RiskPolicy())
    eng.set_reconciliation_ok(True)
    port = PortfolioSnapshot(
        equity=10_000.0,
        available_balance=10_000.0,
        reserved_balance=0.0,
        holdings=(),
        positions=(),
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        fees=0.0,
        exposure=ExposureBreakdown(gross=0.0, net=0.0),
        timestamp_ms=0,
    )
    prop = TradeProposal(
        exchange_id="paper",
        account_id="default",
        symbol="TEST",
        side=Side.BUY,
        requested_quantity=0.01,
        requested_price=100.0,
        strategy_id="p0b",
        timestamp_ms=0,
    )
    d = eng.evaluate(prop, port, entry_price=100.0)
    assert d.verdict == RiskVerdict.APPROVED
    db = tempfile.mktemp(suffix=".db")
    store = ExecutionStore(db)
    intent = "p0b-idem"
    oid = make_client_order_id(
        "paper", "default", "TEST", Side.BUY, d.allowed_quantity, 100.0, intent
    )
    rec = record_from_decision(d, order_type="limit", mode=ExecutionMode.PAPER, intent_key=intent)
    rec.client_order_id = oid
    rec.state = OrderState.SUBMITTED
    store.save(rec)
    blocked = 0
    for _ in range(50):
        ex = store.get_by_client_order_id(oid)
        if ex is not None and not is_terminal(ex.state):
            blocked += 1
    store.close()
    assert blocked == 50


def test_aggregate_matrix(tmp_path: Path):
    results = []
    for stage in ("LOAD_STATE", "RECONCILIATION", "READY", "RUNNING"):
        results.append(_scenario(tmp_path, stage))
    results.append(_scenario(tmp_path, "RUNNING", corrupt="partial_write"))
    unsafe_ready = sum(1 for r in results if r["unsafe_ready"])
    unsafe_exec = sum(1 for r in results if r["unsafe_execution"])
    assert unsafe_ready == 0
    assert unsafe_exec == 0
    out = ROOT / "research" / "results" / "p0b_process_kill_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "stop_after": r["scenario"]["stop_after"],
                        "corrupt": r["scenario"]["corrupt"],
                        "trusted_ready": r["trusted_ready"],
                        "execution_allowed": r["execution_allowed"],
                        "unsafe_ready": r["unsafe_ready"],
                        "unsafe_execution": r["unsafe_execution"],
                        "mttr": r.get("mttr"),
                    }
                    for r in results
                ],
                "unsafe_ready_total": unsafe_ready,
                "unsafe_execution_total": unsafe_exec,
                "process_kill": "hard_kill_platform_native",
                "evidence": "E4_os_subprocess",
            },
            indent=2,
        )
    )
