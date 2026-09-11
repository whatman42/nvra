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
