"""Tokocrypto live preflight — explicit gate report (no orders placed)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from crypto.exchanges.errors import TradingDisabledError
from crypto.exchanges.models import PermissionStatus
from crypto.exchanges.tokocrypto import TokocryptoAdapter
from god.broker.modes import REAL_CONFIRMATION, BrokerMode, BrokerModePolicy


@dataclass(frozen=True, slots=True)
class TokocryptoPreflightReport:
    exchange: str = "tokocrypto"
    mode: str = "DEMO"
    sandbox: bool = True
    allow_real: bool = False
    env_enable: bool = False
    env_confirm: bool = False
    policy_ok: bool = False
    policy_reasons: tuple[str, ...] = ()
    connected: bool = False
    authenticated: bool = False
    can_trade: bool = False
    can_withdraw: bool = False
    withdrawal_blocked_ok: bool = False
    markets_loaded: bool = False
    clock_ok: bool = False
    live_submit_allowed: bool = False
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "mode": self.mode,
            "sandbox": self.sandbox,
            "allow_real": self.allow_real,
            "env_enable": self.env_enable,
            "env_confirm": self.env_confirm,
            "policy_ok": self.policy_ok,
            "policy_reasons": list(self.policy_reasons),
            "connected": self.connected,
            "authenticated": self.authenticated,
            "can_trade": self.can_trade,
            "can_withdraw": self.can_withdraw,
            "withdrawal_blocked_ok": self.withdrawal_blocked_ok,
            "markets_loaded": self.markets_loaded,
            "clock_ok": self.clock_ok,
            "live_submit_allowed": self.live_submit_allowed,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def run_tokocrypto_preflight(
    adapter: TokocryptoAdapter,
    *,
    mode: BrokerMode = BrokerMode.DEMO,
    allow_real: bool = False,
    connect: bool = True,
) -> TokocryptoPreflightReport:
    """Evaluate whether live Tokocrypto submission is allowed. Never places orders."""
    reasons: list[str] = []
    warnings: list[str] = []

    sandbox = bool(getattr(adapter, "_sandbox", True))
    env_enable = os.getenv("NVRA_REAL_TRADING_ENABLE", "0").lower() in {"1", "true", "yes"}
    env_confirm = os.getenv("NVRA_REAL_TRADING_CONFIRM", "") == REAL_CONFIRMATION
    policy = BrokerModePolicy("tokocrypto", mode=mode, sandbox=sandbox, allow_real=allow_real)
    policy_ok, policy_reasons = policy.validate()

    connected = False
    authenticated = False
    can_trade = False
    can_withdraw = False
    markets_loaded = False
    clock_ok = False

    if connect:
        try:
            adapter.connect()
            connected = True
        except Exception as exc:
            reasons.append(f"connect_failed:{type(exc).__name__}")

    try:
        health = adapter.health_check()
        clock_ok = health.name in {"CONNECTED", "DEGRADED"}
    except Exception:
        clock_ok = False

    try:
        report = adapter.validate_permissions()
        authenticated = report.authenticated
        can_trade = report.trading is PermissionStatus.GRANTED
        can_withdraw = report.withdrawal is PermissionStatus.GRANTED
        warnings.extend(report.warnings)
    except Exception as exc:
        reasons.append(f"permission_probe_failed:{type(exc).__name__}")

    try:
        mkts = adapter.fetch_markets()
        markets_loaded = len(mkts) > 0
    except Exception:
        markets_loaded = False

    withdrawal_blocked_ok = not can_withdraw
    if can_withdraw:
        reasons.append("withdrawal_permission_granted_prefer_trade_only_key")

    if mode is BrokerMode.DEMO or sandbox:
        reasons.append("demo_or_sandbox_blocks_production_orders")
    if not policy_ok:
        reasons.extend(policy_reasons)
    if not env_enable:
        reasons.append("NVRA_REAL_TRADING_ENABLE_not_set")
    if not env_confirm:
        reasons.append("real_trading_confirmation_missing")
    if not authenticated:
        reasons.append("not_authenticated")
    if not can_trade:
        reasons.append("trading_permission_not_granted")
    if not clock_ok:
        reasons.append("clock_or_health_not_ok")
    if not markets_loaded:
        reasons.append("markets_not_loaded")

    live_ok = (
        mode is BrokerMode.REAL
        and not sandbox
        and allow_real
        and policy_ok
        and env_enable
        and env_confirm
        and authenticated
        and can_trade
        and withdrawal_blocked_ok
        and clock_ok
        and markets_loaded
        and not any(r.startswith("connect_failed") for r in reasons)
    )

    if live_ok:
        try:
            if adapter.trading_enabled:
                warnings.append("adapter_trading_already_enabled_unexpected")
        except Exception:
            pass

    return TokocryptoPreflightReport(
        mode=mode.value,
        sandbox=sandbox,
        allow_real=allow_real,
        env_enable=env_enable,
        env_confirm=env_confirm,
        policy_ok=policy_ok,
        policy_reasons=tuple(policy_reasons),
        connected=connected,
        authenticated=authenticated,
        can_trade=can_trade,
        can_withdraw=can_withdraw,
        withdrawal_blocked_ok=withdrawal_blocked_ok,
        markets_loaded=markets_loaded,
        clock_ok=clock_ok,
        live_submit_allowed=live_ok,
        reasons=tuple(dict.fromkeys(reasons)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def assert_demo_cannot_submit(adapter: TokocryptoAdapter) -> None:
    """Hard invariant: DEMO/sandbox adapter never hits production create_order."""
    if not getattr(adapter, "_sandbox", True):
        return
    try:
        adapter.create_order("BTC_USDT", "buy", "limit", 0.001, price=1.0)
    except TradingDisabledError:
        return
    raise AssertionError("DEMO Tokocrypto adapter must not allow create_order")
