"""Linux / Oracle deployment contract — no production trading logic changes."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_LINUX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Linux/Oracle deployment filesystem and systemd contract",
)


def _load_entry():
    path = ROOT / "scripts" / "nvrafx_entry.py"
    spec = importlib.util.spec_from_file_location("nvrafx_entry_contract", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nvrafx_entry_contract"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_canonical_product_name_nvra():
    entry = _load_entry()
    assert entry.PRODUCT_NAME == "NVRA"
    assert getattr(entry, "DEVELOPER_NAME", "NUNG") == "NUNG"


def test_headless_entrypoint_no_gui():
    entry = _load_entry()
    import inspect
    body = inspect.getsource(entry._run_headless_autostart)
    assert "run_gui" not in body
    assert "run_autonomous_runtime" in body
    assert "NVRA_DATA_DIR" in body


def test_gui_not_required_for_health():
    entry = _load_entry()
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert entry.cmd_health() == 0
    data = json.loads(buf.getvalue())
    assert data.get("gui_required") is False
    assert data.get("autonomous_headless_supported") is True


def test_autonomous_runtime_importable():
    from god.live.autonomous_runtime import run_autonomous_runtime
    assert callable(run_autonomous_runtime)


@_LINUX_ONLY
def test_policy_path_under_data_dir():
    from god.live.autonomous_policy import POLICY_FILENAME, default_policy_path
    path = default_policy_path(Path("/var/lib/nvra"))
    assert path.name == POLICY_FILENAME
    assert str(path).startswith("/var/lib/nvra")


def test_policy_path_joins_data_dir_cross_platform(tmp_path):
    """Cross-platform: policy path is always under the provided data dir."""
    from god.live.autonomous_policy import POLICY_FILENAME, default_policy_path
    data = tmp_path / "nvra_data"
    path = default_policy_path(data)
    assert path.name == POLICY_FILENAME
    assert path.parent == data


@_LINUX_ONLY
def test_systemd_unit_contract():
    unit = (ROOT / "deploy" / "oracle" / "nvra.service").read_text(encoding="utf-8")
    assert "User=nvra" in unit
    assert "User=root" not in unit
    assert "--autostart --headless" in unit
    assert "Restart=on-failure" in unit
    assert "NVRA_DATA_DIR=/var/lib/nvra" in unit
    unit_section = unit.split("[Service]")[0]
    assert "StartLimitIntervalSec" in unit_section


def test_env_example_has_no_secrets():
    env = (ROOT / "deploy" / "oracle" / "env.example").read_text(encoding="utf-8").lower()
    for bad in ("password=", "api_key=", "secret=", "private_key="):
        assert bad not in env
    assert "nvra_data_dir=" in env


def test_requirements_linux_excludes_windows_only():
    text = (ROOT / "requirements-linux.txt").read_text(encoding="utf-8")
    pkgs = "\n".join(ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    assert "MetaTrader5" not in pkgs
    assert "PySide6" not in pkgs
    assert "pyinstaller" not in pkgs.lower()
    assert "numpy" in pkgs and "ccxt" in pkgs


def test_requirements_windows_mt5_is_platform_gated():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "MetaTrader5" in text
    assert 'platform_system=="Windows"' in text


def test_install_script_markers():
    install = (ROOT / "deploy" / "oracle" / "install.sh").read_text(encoding="utf-8")
    assert "requirements-linux.txt" in install
    assert "0700" in install and "0600" in install
    assert "systemctl enable" in install
