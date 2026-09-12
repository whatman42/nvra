"""Exchange gateway package.

Adapters translate venue APIs into domain models. Live order submission
is gated by ExchangeAdapter.enable_trading + broker mode policy +
ProductionGate. Tokocrypto uses a native REST adapter (no CCXT write path).
"""

from crypto.exchanges.base import ExchangeAdapter
from crypto.exchanges.errors import (
    AuthenticationError,
    CredentialMissingError,
    ExchangeError,
    ExchangeUnavailableError,
    MarketDataError,
    NetworkError,
    PermissionError,
    RateLimitError,
    TradingDisabledError,
    UnsupportedCapabilityError,
)
from crypto.exchanges.factory import create_exchange_adapter, supported_exchanges
from crypto.exchanges.models import (
    AssetBalance,
    ConnectionHealth,
    Market,
    MarketType,
    OHLCVBar,
    OpenOrder,
    OrderBook,
    OrderBookLevel,
    PermissionReport,
    PermissionStatus,
    Position,
    Ticker,
    Trade,
)

__all__ = [
    "ExchangeAdapter",
    "create_exchange_adapter",
    "supported_exchanges",
    # models
    "AssetBalance",
    "ConnectionHealth",
    "Market",
    "MarketType",
    "OHLCVBar",
    "OpenOrder",
    "OrderBook",
    "OrderBookLevel",
    "PermissionReport",
    "PermissionStatus",
    "Position",
    "Ticker",
    "Trade",
    # errors
    "ExchangeError",
    "AuthenticationError",
    "PermissionError",
    "RateLimitError",
    "NetworkError",
    "MarketDataError",
    "UnsupportedCapabilityError",
    "ExchangeUnavailableError",
    "TradingDisabledError",
    "CredentialMissingError",
]
