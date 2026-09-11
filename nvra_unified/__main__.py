from __future__ import annotations
import argparse
import json
import sys

from . import __version__
from .runtime import UnifiedRuntime
from .auth import user_data_dir
from .setup_state import evaluate_setup_state, migrate_legacy_auth_state


def main(argv=None):
    p = argparse.ArgumentParser(prog="NVRA")
    p.add_argument("--version", action="store_true")
    p.add_argument("--health", action="store_true")
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--gui", action="store_true")
    p.add_argument("--no-gui", action="store_true")
    p.add_argument("--setup-status", action="store_true")
    p.add_argument("--migrate-legacy-auth", action="store_true")
    p.add_argument("--register-user", metavar="USERNAME", help=argparse.SUPPRESS)
    a = p.parse_args(argv)

    if a.register_user:
        print(
            "Username registration is removed. NVRA is a single-user local application.\n"
            "Use the Setup Center in the GUI to configure credentials.",
            file=sys.stderr,
        )
        return 2

    if a.migrate_legacy_auth:
        print(json.dumps(migrate_legacy_auth_state(), indent=2))
        return 0

    if a.version:
        print(f"NVRA Unified {__version__}")
        return 0

    if a.smoke:
        r = UnifiedRuntime()
        print(json.dumps({"ok": True, "hardware": r.status.hardware, "home": str(user_data_dir())}, indent=2))
        return 0

    if a.setup_status:
        print(json.dumps(evaluate_setup_state(UnifiedRuntime()).to_dict(), indent=2))
        return 0

    if a.health:
        r = UnifiedRuntime()
        snap = r.snapshot()
        snap["setup"] = evaluate_setup_state(r).to_dict()
        snap["live_authorized"] = False
        snap["execution_mode"] = "PAPER"
        print(json.dumps(snap, indent=2))
        return 0

    if a.gui or not a.no_gui:
        try:
            from .gui import run_gui
            return run_gui()
        except Exception as e:
            print(f"GUI unavailable: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
