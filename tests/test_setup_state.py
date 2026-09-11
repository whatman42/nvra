"""SetupState readiness — single-user, no identity."""
from __future__ import annotations

import builtins
import sys
from unittest.mock import MagicMock

from nvra_unified.setup_state import (
    OverallStatus,
    ServiceStatus,
    evaluate_setup_state,
    mark_setup_complete,
    migrate_legacy_auth_state,
)


def test_fresh_state_not_configured(tmp_path, monkeypatch):
    """Fresh install with zero configured services must be NOT_CONFIGURED.

    Isolate from host environment: Windows CI images ship MetaTrader5, which
    would otherwise count as a useful service and yield PARTIAL. The contract
    under test is configuration readiness of *user* services, not host tooling.
    """
    monkeypatch.setenv("NVRA_HOME", str(tmp_path))

    real_import = builtins.__import__

    def _no_mt5(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "MetaTrader5" or (isinstance(name, str) and name.startswith("MetaTrader5.")):
            raise ImportError("isolated: MetaTrader5 unavailable in fresh-state test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _no_mt5)
    # Also block detect_mt5 if the package path resolves.
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)

    rt = MagicMock()
    rt.secrets.telegram_configured.return_value = False
    rt.secrets.gemini_configured.return_value = False
    rt.secrets.exchange_configured.return_value = False
    rt.secrets.compute_keys_configured.return_value = False
    rt.config.crypto_accounts = []
    rt.config.google_oauth_client_file = ""
    state = evaluate_setup_state(rt)
    assert state.overall == OverallStatus.NOT_CONFIGURED
    assert state.needs_setup() is True
    assert state.telegram == ServiceStatus.NOT_CONFIGURED


def test_partial_with_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("NVRA_HOME", str(tmp_path))
    rt = MagicMock()
    rt.secrets.telegram_configured.return_value = True
    rt.secrets.gemini_configured.return_value = False
    rt.secrets.exchange_configured.return_value = False
    rt.secrets.compute_keys_configured.return_value = False
    rt.config.crypto_accounts = []
    rt.config.google_oauth_client_file = ""
    state = evaluate_setup_state(rt)
    assert state.telegram == ServiceStatus.READY
    assert state.overall in (OverallStatus.PARTIAL, OverallStatus.READY)


def test_mark_setup_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("NVRA_HOME", str(tmp_path))
    mark_setup_complete()
    assert (tmp_path / "setup_complete.flag").is_file()


def test_migrate_legacy_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("NVRA_HOME", str(tmp_path))
    (tmp_path / "auth_verifier.json").write_text("{}", encoding="utf-8")
    info = migrate_legacy_auth_state()
    assert "auth_verifier.json" in info.get("found", []) or info.get("migrated")
