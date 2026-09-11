"""SetupState readiness — single-user, no identity."""
from __future__ import annotations

from unittest.mock import MagicMock

from nvra_unified.setup_state import (
    OverallStatus,
    ServiceStatus,
    evaluate_setup_state,
    mark_setup_complete,
    migrate_legacy_auth_state,
)


def test_fresh_state_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("NVRA_HOME", str(tmp_path))
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
