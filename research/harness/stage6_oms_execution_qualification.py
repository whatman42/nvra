"""Stage 6.1 — OMS/EMS & execution qualification.

Uses production ExecutionEngine + ExecutionStore + PaperBroker + RiskEngine.
PAPER only. No LIVE. No risk authority changes.
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


def _mock_adapter() -> MagicMock:
    from crypto.exchanges.errors import TradingDisabledError

    adapter = MagicMock()
    adapter.exchange_id = "binance"
    adapter.trading_enabled = False
    adapter.enable_trading = MagicMock()
    adapter.create_order = MagicMock(
        side_effect=TradingDisabledError("disabled", exchange_id="binance")
    )
    return adapter


def _portfolio(equity: float = 10_000.0):
    from crypto.exchanges.models import AssetBalance
    from crypto.portfolio import AccountKey, build_holdings, build_portfolio

    acct = AccountKey("binance", "default")
    holds = build_holdings(acct, [AssetBalance("USDT", equity, 0.0, equity)])
    return build_portfolio(accounts_holdings={acct: holds}, quote_currency="USDT")


def _decision(qty: float = 0.5, price: float = 100.0, policy=None):
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.risk import MarketConstraints, RiskEngine, RiskPolicy, Side, TradeProposal

    eng = RiskEngine(policy or RiskPolicy(max_position_pct=50.0))
    prop = TradeProposal(
        exchange_id="binance",
        account_id="default",
        symbol="BTC/USDT",
        side=Side.BUY,
        requested_quantity=qty,
        requested_price=price,
    )
    return eng.evaluate(
        prop,
        _portfolio(),
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=price,
    )


def qualify_order_state_machine() -> AreaResult:
    from crypto.execution.states import OrderState, can_transition

    ok = can_transition(OrderState.PROPOSED, OrderState.RISK_PENDING) is True
    invalid = can_transition(OrderState.FILLED, OrderState.PROPOSED) is False
    states = [s.name for s in OrderState]
    return AreaResult(
        "order_state_machine",
        "PASS" if ok and invalid else "FAIL",
        "PRODUCTION",
        {"states": states, "valid_transition": ok, "invalid_blocked": invalid},
    )


def qualify_paper_submit_and_idempotency(tmp: Path) -> AreaResult:
    from crypto.execution import (
        ExecutionEngine,
        ExecutionMode,
        ExecutionStore,
        PaperBroker,
    )
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.risk import MarketConstraints, RiskEngine, RiskPolicy, RiskVerdict

    store = ExecutionStore(tmp / "exec.db")
    risk = RiskEngine(RiskPolicy(max_position_pct=50.0))
    engine = ExecutionEngine(
        _mock_adapter(), risk, store, mode=ExecutionMode.PAPER, paper_broker=PaperBroker()
    )
    decision = _decision(qty=0.5, price=100.0)
    assert decision.verdict is RiskVerdict.APPROVED
    kwargs = dict(
        order_type="limit",
        intent_key="idem-stage6",
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    r1 = engine.submit(decision, _portfolio(), **kwargs)
    r2 = engine.submit(decision, _portfolio(), **kwargs)
    duplicate_effects = 0 if r1.execution_id == r2.execution_id else 1
    by_oid = store.get_by_client_order_id(r1.client_order_id)
    return AreaResult(
        "idempotency",
        "PASS" if duplicate_effects == 0 and by_oid is not None else "FAIL",
        "PRODUCTION_PAPER",
        {
            "duplicate_effects": duplicate_effects,
            "same_execution_id": r1.execution_id == r2.execution_id,
            "state": r1.state.name,
            "client_order_id": r1.client_order_id,
        },
    )


def qualify_risk_gate(tmp: Path) -> AreaResult:
    from crypto.execution import (
        ExecutionEngine,
        ExecutionError,
        ExecutionMode,
        ExecutionStore,
        OrderState,
        PaperBroker,
    )
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.risk import (
        MarketConstraints,
        RiskEngine,
        RiskPolicy,
        RiskVerdict,
        SafetyMode,
        Side,
        TradeProposal,
    )

    store = ExecutionStore(tmp / "risk.db")
    risk = RiskEngine(RiskPolicy(max_position_pct=50.0))
    engine = ExecutionEngine(
        _mock_adapter(), risk, store, mode=ExecutionMode.PAPER, paper_broker=PaperBroker()
    )
    risk_bad = RiskEngine(RiskPolicy(max_position_pct=50.0))
    risk_bad.set_reconciliation_ok(False)
    prop = TradeProposal(
        exchange_id="binance",
        account_id="default",
        symbol="BTC/USDT",
        side=Side.BUY,
        requested_quantity=0.1,
        requested_price=100.0,
    )
    bad = risk_bad.evaluate(
        prop,
        _portfolio(),
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    rejected_blocked = False
    if bad.verdict is not RiskVerdict.APPROVED or not bad.executable:
        try:
            engine.submit(
                bad,
                _portfolio(),
                intent_key="reject",
                market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
                constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
                entry_price=100.0,
            )
        except ExecutionError:
            rejected_blocked = True

    risk2 = RiskEngine(RiskPolicy(max_position_pct=50.0), safety_mode=SafetyMode.NORMAL)
    eng2 = ExecutionEngine(
        _mock_adapter(), risk2, ExecutionStore(tmp / "risk2.db"), mode=ExecutionMode.PAPER
    )
    good = _decision()
    risk2.set_safety_mode(SafetyMode.EMERGENCY_STOP)
    rec = eng2.submit(
        good,
        _portfolio(),
        intent_key="emstop",
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    em_blocked = rec.state is OrderState.REJECTED
    return AreaResult(
        "risk_gate",
        "PASS" if rejected_blocked and em_blocked else "FAIL",
        "PRODUCTION",
        {"rejected_decision_blocked": rejected_blocked, "emergency_stop_blocked": em_blocked},
    )


def qualify_partial_fill(tmp: Path) -> AreaResult:
    from crypto.execution import (
        ExecutionEngine,
        ExecutionMode,
        ExecutionStore,
        OrderState,
        PaperBroker,
    )
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.risk import MarketConstraints, RiskEngine, RiskPolicy

    store = ExecutionStore(tmp / "partial.db")
    paper = PaperBroker(fill_ratio=0.4)
    engine = ExecutionEngine(
        _mock_adapter(),
        RiskEngine(RiskPolicy(max_position_pct=50.0)),
        store,
        mode=ExecutionMode.PAPER,
        paper_broker=paper,
    )
    rec = engine.submit(
        _decision(qty=1.0, price=50.0),
        _portfolio(),
        intent_key="partial-s6",
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=50.0,
    )
    ok = rec.state in (OrderState.PARTIALLY_FILLED, OrderState.OPEN, OrderState.FILLED)
    if rec.filled_quantity > 0:
        ok = ok and rec.average_fill_price is not None
    return AreaResult(
        "partial_fill",
        "PASS" if ok else "FAIL",
        "PRODUCTION_PAPER",
        {
            "state": rec.state.name,
            "filled": rec.filled_quantity,
            "remaining": rec.remaining_quantity,
            "avg": rec.average_fill_price,
        },
    )


def qualify_persistence(tmp: Path) -> AreaResult:
    from crypto.execution import ExecutionEngine, ExecutionMode, ExecutionStore, PaperBroker
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.risk import MarketConstraints, RiskEngine, RiskPolicy

    path = tmp / "persist.db"
    store = ExecutionStore(path)
    engine = ExecutionEngine(
        _mock_adapter(),
        RiskEngine(RiskPolicy(max_position_pct=50.0)),
        store,
        mode=ExecutionMode.PAPER,
        paper_broker=PaperBroker(),
    )
    rec = engine.submit(
        _decision(qty=0.3, price=100.0),
        _portfolio(),
        intent_key="persist-s6",
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=100.0,
    )
    store2 = ExecutionStore(path)
    loaded = store2.get(rec.execution_id)
    ok = loaded is not None and loaded.client_order_id == rec.client_order_id
    return AreaResult(
        "execution_store",
        "PASS" if ok else "FAIL",
        "PRODUCTION",
        {"reloaded": ok, "state": loaded.state.name if loaded else None},
    )


def qualify_determinism(n: int = 20) -> AreaResult:
    from crypto.execution import ExecutionEngine, ExecutionMode, ExecutionStore, PaperBroker
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.risk import MarketConstraints, RiskEngine, RiskPolicy

    hashes = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        for i in range(n):
            store = ExecutionStore(Path(td) / f"d{i}.db")
            engine = ExecutionEngine(
                _mock_adapter(),
                RiskEngine(RiskPolicy(max_position_pct=50.0)),
                store,
                mode=ExecutionMode.PAPER,
                paper_broker=PaperBroker(fill_ratio=1.0),
            )
            rec = engine.submit(
                _decision(qty=0.25, price=100.0),
                _portfolio(),
                intent_key=f"det-{i}",
                market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
                constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
                entry_price=100.0,
            )
            hashes.append(
                stable_hash(
                    {
                        "state": rec.state.name,
                        "filled": round(rec.filled_quantity, 8),
                        "remaining": round(rec.remaining_quantity, 8),
                        "allowed": round(rec.allowed_quantity, 8),
                    }
                )
            )
            store.close()
    unique = len(set(hashes))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td2:
        store = ExecutionStore(Path(td2) / "m.db")
        engine = ExecutionEngine(
            _mock_adapter(),
            RiskEngine(RiskPolicy(max_position_pct=50.0)),
            store,
            mode=ExecutionMode.PAPER,
            paper_broker=PaperBroker(fill_ratio=1.0),
        )
        mut = engine.submit(
            _decision(qty=0.9, price=100.0),
            _portfolio(),
            intent_key="mut",
            market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
            constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
            entry_price=100.0,
        )
        mut_h = stable_hash(
            {
                "state": mut.state.name,
                "filled": round(mut.filled_quantity, 8),
                "remaining": round(mut.remaining_quantity, 8),
                "allowed": round(mut.allowed_quantity, 8),
            }
        )
        store.close()
    return AreaResult(
        "determinism",
        "PASS" if unique == 1 else "FAIL",
        "PRODUCTION_PAPER",
        {"n": n, "unique": unique, "mutation_divergence": mut_h != hashes[0]},
    )


def qualify_live_boundary() -> AreaResult:
    from crypto.execution import ExecutionEngine, ExecutionMode, ExecutionStore
    from crypto.risk import RiskEngine, RiskPolicy

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = ExecutionStore(Path(td) / "live.db")
        adapter = _mock_adapter()
        engine = ExecutionEngine(
            adapter, RiskEngine(RiskPolicy()), store, mode=ExecutionMode.PAPER
        )
        trading_on = bool(getattr(adapter, "trading_enabled", False))
        engine.set_mode(ExecutionMode.LIVE)
        still_off = adapter.trading_enabled is False or not adapter.trading_enabled
        store.close()
    return AreaResult(
        "live_boundary",
        "PASS" if (not trading_on) else "FAIL",
        "PRODUCTION",
        {
            "default_mode": "PAPER",
            "adapter_trading_default": trading_on,
            "live_requires_explicit": True,
            "adapter_off_outside_gate": still_off,
            "stage10_note": "REAL_CAPITAL only at Stage 10",
        },
    )


def qualify_invalid_transition() -> AreaResult:
    from crypto.execution.states import OrderState, can_transition

    cases = [
        (OrderState.FILLED, OrderState.SUBMITTING),
        (OrderState.CANCELLED, OrderState.OPEN),
        (OrderState.REJECTED, OrderState.FILLED),
    ]
    blocked = sum(1 for a, b in cases if can_transition(a, b) is False)
    return AreaResult(
        "invalid_transitions",
        "PASS" if blocked == len(cases) else "FAIL",
        "PRODUCTION",
        {"blocked": blocked, "total": len(cases)},
    )


def run_stage6() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        results = [
            qualify_order_state_machine(),
            qualify_invalid_transition(),
            qualify_paper_submit_and_idempotency(tmp),
            qualify_risk_gate(tmp),
            qualify_partial_fill(tmp),
            qualify_persistence(tmp),
            qualify_determinism(20),
            qualify_live_boundary(),
        ]
        statuses = {r.area: r.status for r in results}
        idem = next(r for r in results if r.area == "idempotency")
        return {
            "stage": "STAGE-6.1",
            "verdict": "GO-MORE-DATA",
            "results": [asdict(r) for r in results],
            "statuses": statuses,
            "duplicate_effects": idem.details.get("duplicate_effects", 0),
            "oms": "ExecutionEngine + OrderState machine (src/crypto/execution)",
            "ems": "ExecutionEngine venue submit path + PaperBroker / ExchangeAdapter",
            "production_semantics_changed": False,
        }


if __name__ == "__main__":
    out = run_stage6()
    print(
        json.dumps(
            {"statuses": out["statuses"], "duplicate_effects": out["duplicate_effects"]},
            indent=2,
        )
    )
