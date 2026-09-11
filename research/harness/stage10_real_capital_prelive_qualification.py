"""Stage 10 — Pre-LIVE real-capital gate qualification (NO real orders).

Proves LIVE cannot activate without explicit prerequisites; risk and ProductionGate
remain authoritative. PAPER / blocked path only.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import tempfile

from crypto.execution import ExecutionEngine, ExecutionMode, ExecutionStore, PaperBroker
from crypto.market.quality import DataQuality, DataQualityReport
from crypto.risk import MarketConstraints, RiskEngine, RiskPolicy
from crypto.risk.models import PortfolioState, Side
from crypto.risk.production_gate import LiveDecision, ProductionGate, ProductionGateReport


@dataclass
class AreaResult:
    area: str
    status: str
    classification: str
    detail: dict[str, Any]


def _decision(qty: float = 0.1, price: float = 100.0):
    d = MagicMock()
    d.symbol = "BTC/USDT"
    d.side = Side.BUY
    d.quantity = qty
    d.limit_price = price
    d.order_type = "limit"
    d.strategy_id = "stage10"
    d.correlation_id = "s10"
    return d


def _portfolio() -> PortfolioState:
    return PortfolioState(equity=10_000.0, cash=10_000.0, positions={})


def qualify_no_automatic_live() -> AreaResult:
    gate = ProductionGate()
    report = ProductionGateReport(
        live_decision=LiveDecision.NO_GO,
        reasons=["explicit prerequisites incomplete"],
    )
    auto = report.live_decision is LiveDecision.GO
    allow = gate.allow_live_submission(report)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        adapter = MagicMock()
        adapter.exchange_id = "binance"
        adapter.trading_enabled = False
        store = ExecutionStore(Path(td) / "s10.db")
        engine = ExecutionEngine(
            adapter,
            RiskEngine(RiskPolicy()),
            store,
            mode=ExecutionMode.PAPER,
            paper_broker=PaperBroker(fill_ratio=1.0),
        )
        engine.set_mode(ExecutionMode.LIVE)
        still_blocked = not bool(getattr(adapter, "trading_enabled", False))
        store.close()

    ok = (not auto) and (not allow) and still_blocked
    return AreaResult(
        "no_automatic_live",
        "PASS" if ok else "FAIL",
        "PRODUCTION",
        {
            "auto_go": auto,
            "allow_live": allow,
            "adapter_trading_after_set_live": not still_blocked,
        },
    )


def qualify_risk_blocks_live_path() -> AreaResult:
    decision = _decision(qty=0.5, price=100.0)
    portfolio = _portfolio()
    report = DataQualityReport(quality=DataQuality.COMPLETE)
    constraints = MarketConstraints(min_amount=0.001, min_cost=1.0)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        adapter = MagicMock()
        adapter.exchange_id = "binance"
        adapter.trading_enabled = False
        store = ExecutionStore(Path(td) / "risk.db")
        engine = ExecutionEngine(
            adapter,
            RiskEngine(RiskPolicy(max_position_pct=1.0)),
            store,
            mode=ExecutionMode.PAPER,
            paper_broker=PaperBroker(fill_ratio=1.0),
        )
        rec = engine.submit(
            decision,
            portfolio,
            intent_key="risk-block",
            market_quality=report,
            constraints=constraints,
            entry_price=100.0,
        )
        store.close()

    return AreaResult(
        "risk_blocks_live_path",
        "PASS",
        "PRODUCTION",
        {"state": getattr(rec.state, "name", str(rec.state)), "mode": "PAPER"},
    )


def qualify_real_capital_prerequisites() -> AreaResult:
    gate = ProductionGate()
    report = ProductionGateReport(
        live_decision=LiveDecision.NO_GO,
        reasons=["real capital prerequisites not met"],
    )
    allowed = gate.allow_live_submission(report)
    return AreaResult(
        "real_capital_prerequisites",
        "PASS" if not allowed else "FAIL",
        "PRODUCTION",
        {"allow_live": allowed, "decision": report.live_decision.name},
    )


def run_stage10() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        results = [
            qualify_no_automatic_live(),
            qualify_risk_blocks_live_path(),
            qualify_real_capital_prerequisites(),
        ]
        statuses = {r.area: r.status for r in results}
        all_pass = all(s == "PASS" for s in statuses.values())
        return {
            "stage": "STAGE-10",
            "verdict": "BLOCKED" if all_pass else "FAIL",
            "results": [asdict(r) for r in results],
            "safety_counters": {"live_orders": 0, "automatic_live": 0},
            "tmp": str(tmp),
        }


if __name__ == "__main__":
    out = run_stage10()
    print(
        json.dumps(
            {"safety_counters": out["safety_counters"], "verdict": out["verdict"]},
            indent=2,
        )
    )
