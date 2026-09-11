#!/usr/bin/env python3
"""NVRA — single product entry (one-file Windows EXE).

Internal modules remain god.* / N.U.N.G. architecture.
Distributed binary name is only NVRA.exe (not NVRAFX.exe, not NUNG.exe).

Product: NVRA | Developer/Publisher: NUNG (identity only — never a credential).
LIVE capital is BLOCKED by default.
Production GUI: nvra_unified.gui (single-user, no login gate).
Legacy god.gui.main is NOT used by this entrypoint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from getpass import getpass
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRODUCT_NAME = "NVRA"
DEVELOPER_NAME = "NUNG"
PRODUCT_VERSION = "1.0.0"
RUNTIME_VERSION = "1.0.0"
BUILD_ID = "nvra-onefile"


def _cli_write(text: str, *, stream: str = "stdout") -> None:
    target = sys.stdout if stream == "stdout" else sys.stderr
    try:
        if target is None:
            return
        target.write(text)
        try:
            target.flush()
        except (OSError, ValueError, AttributeError):
            pass
    except (OSError, ValueError, AttributeError):
        if stream == "stdout":
            try:
                err = sys.stderr
                if err is not None:
                    err.write(text)
                    try:
                        err.flush()
                    except (OSError, ValueError, AttributeError):
                        pass
            except (OSError, ValueError, AttributeError):
                pass


def _version_text() -> str:
    return (
        f"NVRA\n"
        f"Developed by {DEVELOPER_NAME}\n"
        f"Product Version: {PRODUCT_VERSION}\n"
        f"Runtime Version: {RUNTIME_VERSION}\n"
        f"Build ID: {BUILD_ID}\n"
        f"Architecture: N.U.N.G. / GOD (internal)\n"
        f"Default mode: PAPER\n"
        f"Live trading: disabled by default\n"
        f"Executable: NVRA.exe (single product binary)\n"
        f"GUI: single-user local (no login gate)\n"
    )


def cmd_version() -> int:
    _cli_write(_version_text())
    return 0


def cmd_health() -> int:
    payload = {
        "product": PRODUCT_NAME,
        "developer": DEVELOPER_NAME,
        "state": "READY",
        "gui_required": False,
        "live_trading_enabled": False,
        "live_authorized": False,
        "autonomous_headless_supported": True,
        "broker_orders_submitted": 0,
        "executable": "NVRA.exe",
        "gui": "nvra_unified.gui",
        "auth_gate": False,
    }
    _cli_write(json.dumps(payload, indent=2) + "\n")
    return 0


def cmd_check_config() -> int:
    try:
        from god.production import default_paper_config, validate_config
        from god.production.validation import ConfigValidationStatus

        cfg = default_paper_config()
        res = validate_config(cfg)
        out = {
            "status": res.status.value,
            "reasons": list(res.reasons),
            "live_trading_enabled": False,
            "live_authorized": False,
            "broker_orders_submitted": 0,
            "product": PRODUCT_NAME,
        }
        _cli_write(json.dumps(out, indent=2) + "\n")
        return 0 if res.status == ConfigValidationStatus.VALID else 1
    except Exception as exc:
        out = {
            "status": "ERROR",
            "reasons": [str(exc)],
            "live_trading_enabled": False,
            "live_authorized": False,
            "broker_orders_submitted": 0,
            "product": PRODUCT_NAME,
        }
        _cli_write(json.dumps(out, indent=2) + "\n")
        return 1


def _run_nung_app(argv: list[str]) -> int:
    """Legacy CLI subcommands retained for tests; not used by production GUI path."""
    from god.app import NungApplication

    parser = argparse.ArgumentParser(prog="NVRA")
    parser.add_argument("--data-dir", default=str(Path.home() / ".nvrafx"))
    sub = parser.add_subparsers(dest="cmd")

    p_reg = sub.add_parser("register", help="Register using hidden password prompt; password is never stored in argv")
    p_reg.add_argument("username")
    p_reg.add_argument("--display-name", default="")

    p_login = sub.add_parser("login", help="Login using hidden password prompt; password is never stored in argv")
    p_login.add_argument("username")

    p_status = sub.add_parser("status")
    p_status.add_argument("--token-file", help="Read session token from a protected file")

    p_start = sub.add_parser("start")
    p_start.add_argument("--token-file", help="Read session token from a protected file")

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--token-file", help="Read session token from a protected file")

    args = parser.parse_args(argv)

    def _token() -> str:
        path = getattr(args, "token_file", None)
        if path:
            return Path(path).read_text(encoding="utf-8").strip()
        env = os.environ.get("NVRA_SESSION_TOKEN", "").strip()
        if env:
            return env
        parser.error("a token file or NVRA_SESSION_TOKEN is required")
        return ""

    def _password() -> str:
        return getpass("Password: ")
    app = NungApplication(Path(args.data_dir))

    if args.cmd == "register":
        print(json.dumps(app.register(args.username, _password(), args.display_name)))
        return 0
    if args.cmd == "login":
        print(json.dumps(app.login(args.username, _password())))
        return 0
    if args.cmd == "status":
        try:
            app.require_auth(_token())
        except Exception as e:
            print(json.dumps({"ok": False, "reason": str(e)}))
            return 1
        print(json.dumps(app.dashboard()))
        return 0
    if args.cmd == "start":
        print(json.dumps(app.start(_token())))
        return 0
    if args.cmd == "stop":
        print(json.dumps(app.stop(_token())))
        return 0

    parser.print_help()
    return 1


def _run_gui(*, autostart_mode: bool = False) -> int:
    """Production single-user GUI — no username/password/login gate.

    Uses nvra_unified.gui exclusively. Legacy god.gui.main is not imported.
    """
    try:
        from nvra_unified.gui import run_gui
        return int(run_gui() or 0)
    except Exception as exc:
        _cli_write(f"GUI startup failed: {exc}\n", stream="stderr")
        return 1


def _run_headless_autostart() -> int:
    """Autonomous core — no GUI, no login, no interactive ARM."""
    try:
        from pathlib import Path as _P
        from god.live.autonomous_runtime import run_autonomous_runtime
        data = os.environ.get("NVRA_DATA_DIR", "").strip()
        data_dir = _P(data) if data else None
        return int(run_autonomous_runtime(data_dir=data_dir))
    except Exception as exc:
        _cli_write(f"Headless autostart failed: {exc}\n", stream="stderr")
        return 1


def cmd_diagnose_mt5() -> int:
    """Import/diagnostic MetaTrader5 inside this runtime. Never places orders. Never enables LIVE."""
    payload: dict = {
        "python_module": "missing",
        "initialize": "not_attempted",
        "live_authorized": False,
        "orders": False,
        "error": "",
    }
    try:
        import MetaTrader5 as mt5  # noqa: F401
        payload["python_module"] = "available"
    except ModuleNotFoundError as exc:
        payload["error"] = f"python_module_missing:{exc}"
        _cli_write(json.dumps(payload, indent=2) + "\n")
        return 2
    except Exception as exc:
        payload["error"] = f"import_error:{type(exc).__name__}"
        _cli_write(json.dumps(payload, indent=2) + "\n")
        return 2
    try:
        from god.broker.mt5.diagnose_api import diagnose_mt5
        diag = diagnose_mt5()
        payload.update(diag)
        payload["live_authorized"] = False
        payload["orders"] = False
    except Exception as exc:
        payload["error"] = f"diagnose_exception:{type(exc).__name__}:{exc}"
        payload["python_module"] = "available"
    _cli_write(json.dumps(payload, indent=2) + "\n")
    return 0 if payload.get("python_module") == "available" else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="NVRA",
        description="NVRA — single product runtime (Developed by NUNG; paper/DEMO default)",
    )
    p.add_argument("--version", action="store_true", help="Show product/runtime version")
    p.add_argument("--health", action="store_true", help="Report health (no secrets)")
    p.add_argument("--check-config", action="store_true", help="Validate configuration")
    p.add_argument("--gui", action="store_true", help="Launch the desktop GUI (optional observer)")
    p.add_argument("--autostart", action="store_true", help="Start autonomous runtime after administrative setup")
    p.add_argument("--headless", action="store_true", help="No GUI (required for production auto-start)")
    p.add_argument("--diagnose-mt5", action="store_true", help="Diagnose MetaTrader5 package/terminal (no orders)")
    p.add_argument(
        "--help-full",
        action="store_true",
        help="Show extended help including app subcommands",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("register", "login", "status", "start", "stop"):
        return _run_nung_app(argv)

    parser = build_parser()
    args, _unknown = parser.parse_known_args(argv)

    if args.autostart and args.headless:
        return _run_headless_autostart()
    if args.autostart and not args.headless:
        return _run_gui(autostart_mode=True)
    if args.headless and not args.autostart:
        return _run_headless_autostart()
    if args.gui:
        return _run_gui()
    if args.version:
        return cmd_version()
    if args.health:
        return cmd_health()
    if getattr(args, "diagnose_mt5", False):
        return cmd_diagnose_mt5()
    if args.check_config:
        return cmd_check_config()
    if args.help_full:
        parser.print_help()
        print("\nApp subcommands: register | login | status | start | stop")
        return 0

    if not argv:
        return _run_gui()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
