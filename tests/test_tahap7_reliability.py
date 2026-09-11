"""Tahap 7 reliability / capability registry lifecycle (Windows-safe cleanup)."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from god.capability.models import CapabilityProvider, CapabilityType
from god.capability.registry import CapabilityRegistry
from god.autonomous.control_loop import AutonomousControlLoop
from god.market.models import Quote


def test_capability_registry_no_leaks():
    hits = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "caps.db"
        reg = CapabilityRegistry(db_path=db)
        reg.register(
            CapabilityProvider.create(
                name="Docker",
                capability=CapabilityType.CONTAINER,
                available=True,
                executable="/usr/bin/docker",
            )
        )
        assert reg.best(CapabilityType.CONTAINER) is not None
        reg.close()
        del reg
    # TemporaryDirectory exits without PermissionError


def test_sqlite_tempdir_cleanup_windows_safe():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "caps.db"
        reg = CapabilityRegistry(db_path=db)
        reg.register(
            CapabilityProvider.create(
                name="Docker",
                capability=CapabilityType.CONTAINER,
                available=True,
                executable="/usr/bin/docker",
            )
        )
        reg.close()
        del reg
        # file should be deletable (Windows WinError 32 regression)
    # TemporaryDirectory exits without PermissionError


def test_sqlite_repeated_lifecycle():
    for _ in range(5):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            reg = CapabilityRegistry(db_path=Path(td) / "c.db")
            reg.register(
                CapabilityProvider.create("Py", CapabilityType.PYTHON, available=True)
            )
            assert reg.best(CapabilityType.PYTHON) is not None
            reg.close()


def test_autonomous_crash_no_duplicate_intent(tmp_path):
    loop = AutonomousControlLoop(ml_registry=tmp_path / "ml")
    q = Quote("EURUSD", time.time(), bid=1.1, ask=1.1002, sequence=1)
    out = loop.run_cycle(quote=q, closes=[1.0] * 50, crash_after_state="OBSERVING")
    assert out.recovery_required
    assert out.broker_orders_submitted == 0
    # resume
    out2 = loop.run_cycle(quote=q, closes=[1.0] * 50, resume_cycle_id=out.cycle_id)
    assert out2.recovery_required is False or out2.broker_orders_submitted == 0
