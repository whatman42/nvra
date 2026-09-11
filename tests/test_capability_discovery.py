"""Unit tests for Capability Discovery Layer.

These tests run on Linux CI and Windows hosts. Windows-specific probes
are exercised when available; otherwise they degrade gracefully.
"""

from __future__ import annotations

import json
import platform
import tempfile
from pathlib import Path

import pytest

from god.capability.models import CapabilityProvider, CapabilityType
from god.capability.registry import CapabilityRegistry
from god.capability.discovery import CapabilityDiscovery
from god.capability import probes


class TestModels:
    def test_provider_create(self):
        p = CapabilityProvider.create(
            name="TestBrowser",
            capability=CapabilityType.BROWSER,
            available=True,
            executable="/usr/bin/chrome",
            version="120.0",
            interface="browser_automation",
        )
        assert p.available is True
        assert p.health == "healthy"
        assert p.provider_id
        d = p.to_dict()
        assert d["capability"] == "browser"
        assert d["name"] == "TestBrowser"

    def test_mark_used_success_and_failure(self):
        p = CapabilityProvider.create(
            name="Shell",
            capability=CapabilityType.SHELL,
            available=True,
        )
        p.mark_used(success=True, latency_ms=12.5)
        assert p.usage_count == 1
        assert p.latency_ms == 12.5
        assert p.success_rate > 0.9

        for _ in range(12):
            p.mark_used(success=False)
        assert p.failure_count >= 10
        assert p.health == "unavailable"
        assert p.available is False

    def test_best_provider_selection(self):
        from god.capability.models import Capability

        good = CapabilityProvider.create("Good", CapabilityType.SHELL, available=True)
        good.success_rate = 0.95
        bad = CapabilityProvider.create("Bad", CapabilityType.SHELL, available=True)
        bad.success_rate = 0.4
        dead = CapabilityProvider.create("Dead", CapabilityType.SHELL, available=False)

        cap = Capability(capability=CapabilityType.SHELL, providers=[bad, dead, good])
        best = cap.best_provider()
        assert best is not None
        assert best.name == "Good"


class TestRegistry:
    def test_register_and_query(self):
        reg = CapabilityRegistry()
        p = CapabilityProvider.create(
            name="Git",
            capability=CapabilityType.VCS,
            available=True,
            executable="/usr/bin/git",
        )
        reg.register(p)
        assert reg.best(CapabilityType.VCS) is not None
        assert reg.best("vcs").name == "Git"
        assert reg.get_provider("Git", CapabilityType.VCS) is not None

    def test_deduplicate_by_name(self):
        reg = CapabilityRegistry()
        p1 = CapabilityProvider.create("Git", CapabilityType.VCS, available=False)
        p2 = CapabilityProvider.create("Git", CapabilityType.VCS, available=True, executable="/bin/git", version="2.40")
        reg.register(p1)
        reg.register(p2)
        providers = reg.get_capability(CapabilityType.VCS).providers
        assert len(providers) == 1
        assert providers[0].available is True
        assert providers[0].version == "2.40"

    def test_sqlite_persistence(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            db = Path(td) / "caps.db"
            reg1 = CapabilityRegistry(db_path=db)
            p = CapabilityProvider.create(
                name="Docker",
                capability=CapabilityType.CONTAINER,
                available=True,
                executable="/usr/bin/docker",
                version="24.0",
            )
            reg1.register(p)

            reg2 = CapabilityRegistry(db_path=db)
            loaded = reg2.get_provider("Docker", CapabilityType.CONTAINER)
            assert loaded is not None
            assert loaded.available is True
            assert loaded.version == "24.0"
            assert loaded.executable == "/usr/bin/docker"

    def test_snapshot(self):
        reg = CapabilityRegistry()
        reg.register(
            CapabilityProvider.create("Python", CapabilityType.PYTHON, available=True)
        )
        snap = reg.snapshot()
        assert snap["provider_count"] == 1
        assert "python" in snap["capabilities"]


class TestProbes:
    def test_which_python(self):
        exe = probes.which("python3") or probes.which("python")
        assert exe is not None

    def test_run_cmd_echo(self):
        if probes.IS_WINDOWS:
            code, out, err = probes.run_cmd(["cmd", "/c", "echo", "hello"])
        else:
            code, out, err = probes.run_cmd(["echo", "hello"])
        assert code == 0
        assert "hello" in out

    def test_os_info(self):
        info = probes.os_info()
        assert "system" in info
        assert info["system"] == platform.system()

    def test_python_info(self):
        info = probes.python_info()
        assert info["executable"]
        assert info["version"]

    def test_cpu_info(self):
        info = probes.cpu_info()
        assert info.get("count", 0) >= 1


class TestDiscovery:
    def test_scan_returns_registry(self):
        disc = CapabilityDiscovery()
        reg = disc.scan(full=True)
        assert isinstance(reg, CapabilityRegistry)
        assert len(reg.all_providers()) > 0

    def test_python_always_available(self):
        disc = CapabilityDiscovery()
        reg = disc.scan(full=False)
        py = reg.best(CapabilityType.PYTHON)
        assert py is not None
        assert py.available is True
        assert py.executable

    def test_os_capability_present(self):
        disc = CapabilityDiscovery()
        reg = disc.scan(full=False)
        os_cap = reg.get_capability(CapabilityType.OS)
        assert len(os_cap.providers) >= 1
        assert os_cap.providers[0].available is True

    def test_shell_discovered(self):
        disc = CapabilityDiscovery()
        reg = disc.scan(full=False)
        shells = reg.get_capability(CapabilityType.SHELL)
        # At least one shell should be available on any sane system
        available_shells = [p for p in shells.providers if p.available]
        assert len(available_shells) >= 1

    def test_rescan_updates_registry(self):
        disc = CapabilityDiscovery()
        reg = disc.scan(full=False)
        count1 = len(reg.all_providers())
        reg2 = disc.scan(full=False)
        count2 = len(reg2.all_providers())
        # Rescan should not explode provider count (dedup works)
        assert count2 == count1

    def test_snapshot_json_serializable(self):
        disc = CapabilityDiscovery()
        reg = disc.scan(full=True)
        snap = reg.snapshot()
        # Must be JSON-serializable for logging / IPC
        text = json.dumps(snap)
        assert len(text) > 10
