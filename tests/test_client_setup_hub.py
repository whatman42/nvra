"""Single-user Setup Center — no login gate; production GUI contract."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


FORBIDDEN_UI = (
    "Create Account",
    "LOCKED — login required",
    "LOCKED - login required",
    "enrollment_required",
    "login_user",
    "login_pass",
)


def _gui_source() -> str:
    return Path("nvra_unified/gui.py").read_text(encoding="utf-8")


def test_production_gui_has_no_login_controls():
    src = _gui_source()
    for token in FORBIDDEN_UI:
        assert token not in src, f"Forbidden auth UI still present: {token!r}"
    assert 'QLabel("Username")' not in src
    assert 'QLabel("Password")' not in src
    assert 'addRow("Username"' not in src
    assert 'addRow("Password"' not in src
    assert "Create Account" not in src


def test_production_gui_has_setup_center_and_tabs():
    src = _gui_source()
    tree = ast.parse(src)
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "setup_center_tab",
        "dashboard_tab",
        "crypto_tab",
        "forex_tab",
        "idx_tab",
        "telegram_tab",
        "ml_tab",
        "settings_tab",
        "save_gemini",
        "test_gemini",
        "clear_gemini",
        "pick_google_client",
        "save_google_client",
        "connect_google",
        "test_exchange",
        "clear_exchange",
        "save_exchange",
        "test_telegram",
        "clear_telegram",
        "save_telegram",
        "detect_mt5",
        "refresh_setup_status",
        "test_all_safe",
        "finish_setup",
        "_route_startup",
        "run_gui",
    }
    missing = required - methods
    assert not missing, f"Missing production GUI methods: {sorted(missing)}"
    assert "_guard" not in methods
    assert "enroll_account" not in methods


def test_no_logged_in_gate():
    src = _gui_source()
    assert "self.logged_in" not in src
    assert "def _guard" not in src
    assert "if not self.logged_in" not in src


def test_entrypoint_does_not_import_legacy_auth_gui():
    """Production unified entry must not pull god.gui.main (legacy login GUI)."""
    entry = Path("scripts/nvra_unified_entry.py").read_text(encoding="utf-8")
    assert "god.gui.main" not in entry
    assert "nvra_unified" in entry
    main_py = Path("nvra_unified/__main__.py").read_text(encoding="utf-8")
    assert "from .gui import run_gui" in main_py
    assert "god.gui.main" not in main_py


def test_nvrafx_entry_uses_unified_gui():
    """Windows EXE entry must launch nvra_unified.gui, not legacy god.gui.main."""
    src = Path("scripts/nvrafx_entry.py").read_text(encoding="utf-8")
    assert "from nvra_unified.gui import run_gui" in src or "nvra_unified.gui" in src
    assert "from god.gui.main import run_gui" not in src


def test_gui_builds_without_auth_state(tmp_path, monkeypatch):
    """GUI module constructs without enrolled auth state."""
    monkeypatch.setenv("NVRA_HOME", str(tmp_path))
    src = _gui_source()
    assert "NVRAUnifiedWindow" in src
    assert "evaluate_setup_state" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "auth" in (node.module or ""):
            names = {a.name for a in (node.names or [])}
            forbidden = {"login", "create_account", "enroll_first_user", "verify_login", "AuthResult"}
            assert not (names & forbidden), f"GUI must not import auth login APIs: {names & forbidden}"


def test_google_oauth_is_not_login_gate():
    src = _gui_source()
    assert "Connect Google" in src or "connect_google" in src
    assert "Google Sign-in" not in src
