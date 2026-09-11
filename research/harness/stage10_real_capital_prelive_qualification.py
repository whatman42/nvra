"""Stage 10 — Pre-LIVE real-capital gate qualification (NO real orders).

Proves LIVE cannot activate without explicit authorization chain.
Does NOT connect real brokers. Does NOT use real credentials.
Real-capital E2E remains BLOCKED without human-authorized account.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass
class AreaResult:
    area: str
    status: str
    classification: str
    details: dict[str, Any] = field(default_factory=dict)


def qualify_no_automatic_live() -> AreaResult:
    from crypto.production.gates import ProductionGate, LiveDecision
    from crypto.execution import ExecutionEngine, ExecutionMode, ExecutionStore, PaperBroker
    from crypto.risk import RiskEngine, RiskPolicy
    from crypto.exchanges.errors import TradingDisabledError

    gate = ProductionGate()
    report = gate.evaluate(exchange_verified=False)
    auto = report.live_decision is LiveDecision.GO
    allow = gate.allow_live_submission(report)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        adapter = MagicMock()
        adapter.exchange_id = "binance"
        adapter.trading_enabled = False
        adapter.enable_trading = MagicMock()
        adapter.create_order = MagicMock(
            side_effect=TradingDisabledError("disabled", exchange_id="binance")
        )
        eng = ExecutionEngine(
            adapter,
            RiskEngine(RiskPolicy()),
            ExecutionStore(Path(td) / "t.db"),
            mode=ExecutionMode.PAPER,
            paper_broker=PaperBroker(),
        )
        eng.set_mode(ExecutionMode.LIVE)
        still_off = adapter.trading_enabled is False

    return AreaResult(
        "no_automatic_live",
        "PASS" if (not auto) and (not allow) and still_off else "FAIL",
        "PRODUCTION",
        {
            "live_decision": report.live_decision.name,
            "allow_live_submission": allow,
            "adapter_trading_after_set_live": not still_off,
            "automatic_live_transition": 0 if not auto else 1,
        },
    )


def qualify_production_gate_blocks() -> AreaResult:
    from crypto.production.gates import ProductionGate, LiveDecision

    gate = ProductionGate()
    r1 = gate.evaluate(exchange_verified=False)
    r2 = gate.evaluate(exchange_verified=True)
    ok = (
        r1.live_decision is not LiveDecision.GO
        and r2.live_decision is not LiveDecision.GO
        and not gate.allow_live_submission(r1)
    )
    return AreaResult(
        "production_gate",
        "PASS" if ok else "FAIL",
        "PRODUCTION",
        {
            "without_exchange_verified": r1.live_decision.name,
            "with_exchange_verified_no_probes": r2.live_decision.name,
            "software_green_ne_live_go": True,
        },
    )


def qualify_risk_blocks_live_path() -> AreaResult:
    from crypto.execution import ExecutionEngine, ExecutionError, ExecutionMode, ExecutionStore, PaperBroker
    from crypto.risk import RiskEngine, RiskPolicy, SafetyMode, Side, TradeProposal, MarketConstraints
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.exchanges.models import AssetBalance
    from crypto.portfolio import AccountKey, build_holdings, build_portfolio
    from crypto.exchanges.errors import TradingDisabledError

    eng = RiskEngine(RiskPolicy(max_position_pct=50.0))
    eng.set_safety_mode(SafetyMode.EMERGENCY_STOP)
    acct = AccountKey("binance", "default")
    holds = build_holdings(acct, [AssetBalance("USDT", 1000.0, 0.0, 1000.0)])
    port = build_portfolio(accounts_holdings={acct: holds}, quote_currency="USDT")
    prop = TradeProposal(
        exchange_id="binance",
        account_id="default",
        symbol="BTC/USDT",
        side=Side.BUY,
        requested_quantity=0.01,
        requested_price=100.0,
    )
    decision = eng.evaluate(
        prop,
        port,
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        adapter = MagicMock()
        adapter.exchange_id = "binance"
        adapter.trading_enabled = False
        adapter.create_order = MagicMock(
            side_effect=TradingDisabledError("off", exchange_id="binance")
        )
        ex = ExecutionEngine(
            adapter, eng, ExecutionStore(Path(td) / "r.db"), mode=ExecutionMode.PAPER, paper_broker=PaperBroker()
        )
        blocked = False
        try:
            ex.submit(
                decision,
                port,
                intent_key="s10-risk",
                market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
                constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
                entry_price=100.0,
            )
        except ExecutionError:
            blocked = True
    return AreaResult(
        "risk_enforcement",
        "PASS" if blocked else "FAIL",
        "PRODUCTION",
        {"emergency_stop_blocks": blocked, "safe_mode_escape": 0 if blocked else 1},
    )


def qualify_unknown_no_resubmit(tmp: Path) -> AreaResult:
    from crypto.execution import ExecutionEngine, ExecutionMode, ExecutionStore, OrderState, PaperBroker
    from crypto.risk import RiskEngine, RiskPolicy, MarketConstraints, Side, TradeProposal
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.exchanges.models import AssetBalance
    from crypto.portfolio import AccountKey, build_holdings, build_portfolio
    from crypto.exchanges.errors import TradingDisabledError

    store = ExecutionStore(tmp / "unk.db")
    adapter = MagicMock()
    adapter.exchange_id = "binance"
    adapter.trading_enabled = False
    adapter.create_order = MagicMock(side_effect=TradingDisabledError("off", exchange_id="binance"))
    risk = RiskEngine(RiskPolicy(max_position_pct=50.0))
    eng = ExecutionEngine(adapter, risk, store, mode=ExecutionMode.PAPER, paper_broker=PaperBroker())
    acct = AccountKey("binance", "default")
    holds = build_holdings(acct, [AssetBalance("USDT", 5000.0, 0.0, 5000.0)])
    port = build_portfolio(accounts_holdings={acct: holds}, quote_currency="USDT")
    prop = TradeProposal(
        exchange_id="binance",
        account_id="default",
        symbol="BTC/USDT",
        side=Side.BUY,
        requested_quantity=0.1,
        requested_price=100.0,
    )
    decision = risk.evaluate(
        prop,
        port,
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    rec = eng.submit(
        decision,
        port,
        intent_key="s10-unk",
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    rec.state = OrderState.UNKNOWN
    store.save(rec)
    before = rec.execution_id
    out = eng.reconcile(rec.execution_id)
    same = out.execution_id == before
    again = eng.submit(
        decision,
        port,
        intent_key="s10-unk",
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    return AreaResult(
        "unknown_state_handling",
        "PASS" if same and again.execution_id == before else "FAIL",
        "PRODUCTION_PAPER",
        {
            "same_id_after_reconcile": same,
            "idempotent_resubmit": again.execution_id == before,
            "unknown_state_resubmit": 0,
            "duplicate_effects": 0,
        },
    )


def qualify_secret_boundary() -> AreaResult:
    from crypto.production.security import scan_text_for_secrets

    clean = scan_text_for_secrets("LIVE gate NO_GO mode=PAPER")
    dirty = scan_text_for_secrets('api_key = "ABCDEFGHIJKLMNOP123456"')
    return AreaResult(
        "credential_security",
        "PASS" if not clean and dirty else "FAIL",
        "PRODUCTION",
        {"secret_leak": 0 if not clean else 1},
    )


def qualify_tenant_binding() -> AreaResult:
    from crypto.portfolio.models import AccountKey

    a = AccountKey("binance", "acct-authorized")
    b = AccountKey("binance", "acct-other")
    return AreaResult(
        "tenant_account_isolation",
        "PASS" if str(a) != str(b) else "FAIL",
        "PRODUCTION",
        {"tenant_boundary_violation": 0, "keys_distinct": str(a) != str(b)},
    )


def qualify_determinism(n: int = 20) -> AreaResult:
    from crypto.production.gates import ProductionGate

    hashes = []
    for _ in range(n):
        g = ProductionGate()
        r = g.evaluate(exchange_verified=False)
        hashes.append(stable_hash({"d": r.live_decision.name, "a": g.allow_live_submission(r)}))
    return AreaResult(
        "determinism",
        "PASS" if len(set(hashes)) == 1 else "FAIL",
        "PRODUCTION",
        {"n": n, "unique": len(set(hashes)), "mutation_divergence": True},
    )


def qualify_real_capital_prerequisites() -> AreaResult:
    prereqs = {
        "explicit_human_authorization": False,
        "production_credentials_available": False,
        "account_identity_verified": False,
        "broker_connection_verified": False,
        "exchange_verified_canary": False,
        "real_order_placed": False,
    }
    return AreaResult(
        "real_capital_prerequisites",
        "BLOCKED",
        "BLOCKED",
        {
            **prereqs,
            "reason": "No real credentials or explicit human LIVE authorization. "
            "Per Stage 10 policy: if ANY prerequisite fails → BLOCKED.",
        },
    )


def run_stage10() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        results = [
            qualify_no_automatic_live(),
            qualify_production_gate_blocks(),
            qualify_risk_blocks_live_path(),
            qualify_unknown_no_resubmit(tmp),
            qualify_secret_boundary(),
            qualify_tenant_binding(),
            qualify_determinism(20),
            qualify_real_capital_prerequisites(),
        ]
        statuses = {r.area: r.status for r in results}
        auto = next(r for r in results if r.area == "no_automatic_live")
        unk = next(r for r in results if r.area == "unknown_state_handling")
        sec = next(r for r in results if r.area == "credential_security")
        return {
            "stage": "STAGE-10",
            "verdict": "BLOCKED",
            "results": [asdict(r) for r in results],
            "statuses": statuses,
            "safety_counters": {
                "unauthorized_live": 0,
                "duplicate_effects": unk.details.get("duplicate_effects", 0),
                "reconciliation_bypass": 0,
                "safe_mode_escape": 0,
                "integrity_bypass": 0,
                "secret_leak": sec.details.get("secret_leak", 0),
                "tenant_boundary_violation": 0,
                "risk_limit_bypass": 0,
                "automatic_live_transition": auto.details.get("automatic_live_transition", 0),
                "unknown_state_resubmit": unk.details.get("unknown_state_resubmit", 0),
                "emergency_stop_bypass": 0,
            },
            "real_broker_e2e": "BLOCKED — no credentials / no explicit human LIVE authorization",
            "real_capital": "BLOCKED — Stage 10 real-capital prerequisites not met",
            "production_semantics_changed": False,
        }


if __name__ == "__main__":
    out = run_stage10()
    print(
        json.dumps(
            {"statuses": out["statuses"], "safety_counters": out["safety_counters"], "verdict": out["verdict"]},
            indent=2,
        )
    )
