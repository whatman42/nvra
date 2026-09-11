"""Stage 7 — Multi-venue / realistic PAPER execution qualification.

Uses existing ExchangeAdapter boundary, AccountKey multi-exchange identity,
AdversarialPaperBroker profiles, ExecutionEngine + RiskEngine.
No LIVE. No real capital. No authority changes.
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


def _mock_adapter(exchange_id: str = "binance") -> MagicMock:
    from crypto.exchanges.errors import TradingDisabledError

    adapter = MagicMock()
    adapter.exchange_id = exchange_id
    adapter.trading_enabled = False
    adapter.enable_trading = MagicMock()
    adapter.create_order = MagicMock(
        side_effect=TradingDisabledError("disabled", exchange_id=exchange_id)
    )
    return adapter


def _portfolio(equity: float = 10_000.0, exchange_id: str = "binance"):
    from crypto.exchanges.models import AssetBalance
    from crypto.portfolio import AccountKey, build_holdings, build_portfolio

    acct = AccountKey(exchange_id, "default")
    holds = build_holdings(acct, [AssetBalance("USDT", equity, 0.0, equity)])
    return build_portfolio(accounts_holdings={acct: holds}, quote_currency="USDT")


def _decision(
    qty: float = 0.5,
    price: float = 100.0,
    exchange_id: str = "binance",
    symbol: str = "BTC/USDT",
):
    from crypto.market.quality import DataQuality, DataQualityReport
    from crypto.risk import MarketConstraints, RiskEngine, RiskPolicy, Side, TradeProposal

    eng = RiskEngine(policy or RiskPolicy(max_position_pct=50.0) if False else RiskPolicy(max_position_pct=50.0))
    prop = TradeProposal(
        exchange_id=exchange_id,
        account_id="default",
        symbol=symbol,
        side=Side.BUY,
        requested_quantity=qty,
        requested_price=price,
    )
    return eng.evaluate(
        prop,
        _portfolio(exchange_id=exchange_id),
        market_quality=DataQualityReport(quality=DataQuality.COMPLETE),
        constraints=MarketConstraints(min_amount=0.001, min_cost=1.0),
        entry_price=price,
    )
