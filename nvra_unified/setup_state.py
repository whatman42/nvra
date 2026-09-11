"""Single-user configuration readiness — no login identity."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

class ServiceStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    READY = "READY"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"
    OPTIONAL_OFF = "OPTIONAL_OFF"
    UNAVAILABLE = "UNAVAILABLE"

class OverallStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PARTIAL = "PARTIAL"
    READY = "READY"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"

@dataclass
class SetupState:
    telegram: ServiceStatus = ServiceStatus.NOT_CONFIGURED
    ai: ServiceStatus = ServiceStatus.OPTIONAL_OFF
    crypto: ServiceStatus = ServiceStatus.NOT_CONFIGURED
    mt5: ServiceStatus = ServiceStatus.OPTIONAL_OFF
    google: ServiceStatus = ServiceStatus.OPTIONAL_OFF
    compute: ServiceStatus = ServiceStatus.OPTIONAL_OFF
    overall: OverallStatus = OverallStatus.NOT_CONFIGURED
    notes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram": self.telegram.value, "ai": self.ai.value,
            "crypto": self.crypto.value, "mt5": self.mt5.value,
            "google": self.google.value, "compute": self.compute.value,
            "overall": self.overall.value, "notes": dict(self.notes),
            "needs_setup": self.needs_setup(),
        }

    def needs_setup(self) -> bool:
        return self.overall in (
            OverallStatus.NOT_CONFIGURED, OverallStatus.PARTIAL, OverallStatus.INVALID,
        )

def evaluate_setup_state(runtime: Any) -> SetupState:
    secrets, config, state = runtime.secrets, runtime.config, SetupState()
    state.telegram = ServiceStatus.READY if secrets.telegram_configured() else ServiceStatus.NOT_CONFIGURED
    state.notes["telegram"] = "configured" if state.telegram == ServiceStatus.READY else "not_configured"
    state.ai = ServiceStatus.READY if secrets.gemini_configured() else ServiceStatus.OPTIONAL_OFF
    state.notes["ai"] = "gemini_configured" if state.ai == ServiceStatus.READY else "optional_not_configured"
    brokers = ("binance", "tokocrypto", "indodax")
    any_crypto = any(secrets.exchange_configured(b) for b in brokers)
    if not any_crypto:
        for a in getattr(config, "crypto_accounts", []) or []:
            if secrets.exchange_configured(a.broker, a.account_id):
                any_crypto = True
                break
    state.crypto = ServiceStatus.READY if any_crypto else ServiceStatus.NOT_CONFIGURED
    state.notes["crypto"] = "configured" if any_crypto else "not_configured"
    py_ok = False
    try:
        import MetaTrader5  # noqa: F401
        py_ok = True
    except Exception:
        pass
    term_ok = False
    try:
        from god.mt5_runtime.detect import detect_mt5
        term_ok = bool(getattr(detect_mt5(), "found", False))
    except Exception:
        pass
    if py_ok and term_ok:
        state.mt5 = ServiceStatus.READY
        state.notes["mt5"] = "module_and_terminal"
    elif py_ok or term_ok:
        state.mt5 = ServiceStatus.DEGRADED
        state.notes["mt5"] = "module" if py_ok else "terminal_only"
    else:
        state.mt5 = ServiceStatus.OPTIONAL_OFF
        state.notes["mt5"] = "not_detected"
    gpath = (getattr(config, "google_oauth_client_file", "") or "").strip()
    state.google = ServiceStatus.READY if gpath and Path(gpath).is_file() else ServiceStatus.OPTIONAL_OFF
    state.notes["google"] = "client_json_configured" if state.google == ServiceStatus.READY else "optional_not_configured"
    if hasattr(secrets, "compute_keys_configured") and secrets.compute_keys_configured():
        state.compute = ServiceStatus.READY
        state.notes["compute"] = "signing_keys_configured"
    else:
        state.compute = ServiceStatus.OPTIONAL_OFF
        state.notes["compute"] = "optional_local_default"
    useful = sum([
        state.telegram == ServiceStatus.READY,
        state.crypto == ServiceStatus.READY,
        state.mt5 in (ServiceStatus.READY, ServiceStatus.DEGRADED),
        state.ai == ServiceStatus.READY,
    ])
    if _setup_complete_marker():
        state.overall = OverallStatus.READY
    elif useful == 0:
        state.overall = OverallStatus.NOT_CONFIGURED
    elif useful >= 2:
        state.overall = OverallStatus.READY
    else:
        state.overall = OverallStatus.PARTIAL
    return state

def _setup_complete_marker() -> bool:
    try:
        from .auth import user_data_dir
        return (user_data_dir() / "setup_complete.flag").is_file()
    except Exception:
        return False

def mark_setup_complete(runtime: Any = None) -> None:
    try:
        from .auth import user_data_dir
        p = user_data_dir() / "setup_complete.flag"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("ok\n", encoding="utf-8")
    except Exception:
        pass

def migrate_legacy_auth_state() -> dict[str, Any]:
    info: dict[str, Any] = {"migrated": False, "found": []}
    try:
        from .auth import user_data_dir
        home = user_data_dir()
        for name in ("auth_verifier.json", "users.json", "session.json"):
            path = home / name
            if path.is_file():
                info["found"].append(name)
                bak = home / f"{name}.legacy_ignored"
                if not bak.exists():
                    try:
                        path.rename(bak)
                        info["migrated"] = True
                    except Exception:
                        info["notes"] = "rename_failed"
    except Exception as exc:
        info["error"] = type(exc).__name__
    return info
