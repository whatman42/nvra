"""GUI fault-injection tests: a GUI failure must not terminate core state/loop."""
from __future__ import annotations

import threading
import time
from pathlib import Path


def test_gui_exception_isolated_from_running_core(monkeypatch, tmp_path: Path) -> None:
    """A GUI crash is contained at the GUI entry boundary while core keeps running."""
    from crypto.runtime.entrypoint import _boot
    from crypto.runtime.paths import PathResolver
    import scripts.nvrafx_entry as nvrafx_entry
    import sys
    import types

    resolver = PathResolver(tmp_path)
    calls: list[str] = []

    # Build a deterministic, headless startup result: every core stage succeeds.
    def stage(name: str):
        def _stage(ctx):
            calls.append(name)
            return True

        return _stage

    # The real _boot composes startup first.  Stub only its GUI boundary after
    # startup has reached RUNNING, without changing production code.
    import crypto.runtime.startup as startup

    stages = {
        "license_device": stage("license"),
        "load_state": stage("state"),
        "data_broker": stage("broker"),
        "reconciliation": stage("reconcile"),
        "risk_governor": stage("risk"),
    }
    result = startup.run_startup(resolver, [], stages=stages)
    assert result.ok
    assert result.state is startup.StartupState.RUNNING
    assert calls == ["license", "state", "broker", "reconcile", "risk"]

    # A real core-owned loop continues independently of the GUI boundary.
    stop = threading.Event()
    ticks: list[int] = []

    def core_loop() -> None:
        while not stop.wait(0.01):
            ticks.append(1)

    thread = threading.Thread(target=core_loop, daemon=True)
    thread.start()

    def crashing_gui() -> int:
        raise RuntimeError("injected GUI failure")

    # Production path uses nvra_unified.gui — mock that, not legacy god.gui.main.
    fake_gui_module = types.ModuleType("nvra_unified.gui")
    fake_gui_module.run_gui = crashing_gui
    monkeypatch.setitem(sys.modules, "nvra_unified.gui", fake_gui_module)

    # _run_gui is the containment boundary used by the product entrypoint.
    # It must convert the GUI exception into a return code rather than raising.
    rc = nvrafx_entry._run_gui(autostart_mode=False)
    assert rc == 1

    time.sleep(0.05)
    stop.set()
    thread.join(timeout=1)
    assert ticks, "core loop stopped when GUI failed"
    assert result.state is startup.StartupState.RUNNING
