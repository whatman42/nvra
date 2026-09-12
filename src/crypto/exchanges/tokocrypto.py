"""Tokocrypto native REST adapter.

Tokocrypto does **not** provide a native sandbox/testnet. Therefore:

* DEMO / sandbox=True → paper execution stays inside NVRA PaperBroker;
  this adapter never submits production orders while sandbox is True.
* REAL / sandbox=False → live REST orders only after ExchangeAdapter.enable_trading
  (which itself requires NVRA_REAL_TRADING_ENABLE + confirmation phrase)
  and the broader ProductionGate / RiskGovernor path.

Public market endpoints may be used for metadata. Private account reads are
allowed when credentials exist; order write path is hard-gated.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal, ROUND_DOWN
from typing import Any

from crypto.core.credentials import CredentialStore
from crypto.exchanges.base import ExchangeAdapter
from crypto.exchanges.errors import (
    AuthenticationError,
    CredentialMissingError,
    ExchangeError,
    NetworkError,
    PermissionError,
    RateLimitError,
    TradingDisabledError,
    UnsupportedCapabilityError,
)
from crypto.exchanges.models import (
    AssetBalance,
    ConnectionHealth,
    Market,
    MarketType,
    OHLCVBar,
    OpenOrder,
    OrderBook,
    PermissionReport,
    PermissionStatus,
    Position,
    Ticker,
    Trade,
)
from crypto.exchanges.tokocrypto_rest import (
    DEFAULT_BASE_URL,
    TYPE_LIMIT,
    TYPE_MARKET,
    TIME_IN_FORCE_GTC,
    TokocryptoRestClient,
    decimal_str,
    map_order_type,
    map_side,
    redact,
)

logger = logging.getLogger(__name__)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except Exception:
        return None


def _quantize(amount: Decimal, step: Decimal | None) -> Decimal:
    if step is None or step <= 0:
        return amount
    n = (amount / step).to_integral_value(rounding=ROUND_DOWN)
    return n * step


class TokocryptoAdapter(ExchangeAdapter):
    """Native Tokocrypto gateway (REST/HMAC). Not CCXT-backed."""

    exchange_id = "tokocrypto"

    def __init__(
        self,
        credential_store: CredentialStore,
        account_id: str = "default",
        *,
        sandbox: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        transport: Any | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        self._store = credential_store
        self._account_id = account_id
        self._sandbox = bool(sandbox)
        self._base_url = base_url
        self._transport = transport
        self._clock_ms = clock_ms
        self._client: TokocryptoRestClient | None = None
        self._health = ConnectionHealth.DISCONNECTED
        self._markets: dict[str, Market] = {}
        self._raw_symbols: dict[str, dict[str, Any]] = {}
        self._trading_enabled = False

    def connect(self) -> None:
        if self._client is not None and self._health is ConnectionHealth.CONNECTED:
            return
        self._health = ConnectionHealth.CONNECTING
        try:
            creds = self._load_credentials()
            self._client = TokocryptoRestClient(
                creds.api_key.get_secret_value(),
                creds.api_secret.get_secret_value(),
                base_url=self._base_url,
                transport=self._transport,
                clock_ms=self._clock_ms,
            )
            try:
                self._client.sync_clock()
            except Exception as exc:
                logger.warning("tokocrypto clock sync failed: %s", redact(str(exc)))
            self._load_markets()
            self._client.account_spot()
            self._health = ConnectionHealth.CONNECTED
        except CredentialMissingError:
            self._health = ConnectionHealth.AUTH_FAILED
            raise
        except AuthenticationError:
            self._health = ConnectionHealth.AUTH_FAILED
            raise
        except NetworkError:
            self._health = ConnectionHealth.EXCHANGE_UNAVAILABLE
            raise
        except RateLimitError:
            self._health = ConnectionHealth.RATE_LIMITED
            raise
        except Exception as exc:
            self._health = ConnectionHealth.UNKNOWN
            raise ExchangeError(redact(str(exc)), exchange_id=self.exchange_id) from exc

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._health = ConnectionHealth.DISCONNECTED
        self._trading_enabled = False

    def health_check(self) -> ConnectionHealth:
        if self._client is None:
            return ConnectionHealth.DISCONNECTED
        try:
            self._client.server_time()
            if self._health is not ConnectionHealth.CONNECTED:
                self._health = ConnectionHealth.CONNECTED
            return self._health
        except AuthenticationError:
            self._health = ConnectionHealth.AUTH_FAILED
            return self._health
        except NetworkError:
            self._health = ConnectionHealth.EXCHANGE_UNAVAILABLE
            return self._health
        except Exception:
            self._health = ConnectionHealth.DEGRADED
            return self._health

    def validate_permissions(self) -> PermissionReport:
        warnings: list[str] = []
        authenticated = False
        account_read = PermissionStatus.UNKNOWN
        trading = PermissionStatus.UNKNOWN
        withdrawal = PermissionStatus.UNKNOWN
        market_data = PermissionStatus.UNKNOWN
        try:
            self._ensure_client()
            assert self._client is not None
            try:
                self._client.server_time()
                market_data = PermissionStatus.GRANTED
            except Exception:
                market_data = PermissionStatus.DENIED
            try:
                acct = self._client.account_spot()
                authenticated = True
                account_read = PermissionStatus.GRANTED
                can_trade = acct.get("canTrade")
                can_withdraw = acct.get("canWithdraw")
                if can_trade is not None:
                    trading = (
                        PermissionStatus.GRANTED
                        if int(can_trade) == 1
                        else PermissionStatus.DENIED
                    )
                if can_withdraw is not None:
                    withdrawal = (
                        PermissionStatus.GRANTED
                        if int(can_withdraw) == 1
                        else PermissionStatus.DENIED
                    )
                if withdrawal is PermissionStatus.GRANTED:
                    warnings.append(
                        "API key reports withdrawal permission; prefer trade-only keys"
                    )
            except AuthenticationError:
                authenticated = False
                account_read = PermissionStatus.DENIED
            except PermissionError:
                account_read = PermissionStatus.DENIED
            except Exception:
                account_read = PermissionStatus.UNKNOWN
        except Exception as exc:
            warnings.append(redact(str(exc)))
        if self._sandbox:
            warnings.append(
                "sandbox/DEMO mode: production order submit is blocked; "
                "use internal paper simulator for simulated fills"
            )
        return PermissionReport(
            authenticated=authenticated,
            market_data=market_data,
            account_read=account_read,
            trading=trading,
            withdrawal=withdrawal,
            warnings=tuple(warnings),
        )

    def fetch_markets(self) -> Sequence[Market]:
        self._ensure_client()
        if not self._markets:
            self._load_markets()
        return list(self._markets.values())

    def fetch_balance(self) -> Sequence[AssetBalance]:
        self._ensure_client()
        assert self._client is not None
        acct = self._client.account_spot()
        assets = acct.get("accountAssets") or acct.get("balances") or []
        out: list[AssetBalance] = []
        if isinstance(assets, list):
            for row in assets:
                if not isinstance(row, dict):
                    continue
                asset = str(row.get("asset") or row.get("coin") or "")
                if not asset:
                    continue
                free = _to_float(row.get("free"))
                locked = _to_float(row.get("locked") or row.get("used"))
                total = None
                if free is not None and locked is not None:
                    total = free + locked
                elif free is not None:
                    total = free
                out.append(AssetBalance(asset=asset, free=free, used=locked, total=total))
        return out

    def fetch_ticker(self, symbol: str) -> Ticker:
        raise UnsupportedCapabilityError(
            "fetch_ticker via native path not fully specified for all symbol types",
            exchange_id=self.exchange_id,
        )

    def fetch_order_book(self, symbol: str, limit: int | None = None) -> OrderBook:
        raise UnsupportedCapabilityError(
            "fetch_order_book native path requires symbolType-specific base URLs",
            exchange_id=self.exchange_id,
        )

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Sequence[OHLCVBar]:
        raise UnsupportedCapabilityError(
            "fetch_ohlcv native path requires symbolType-specific base URLs",
            exchange_id=self.exchange_id,
        )

    def fetch_open_orders(self, symbol: str | None = None) -> Sequence[OpenOrder]:
        self._ensure_client()
        assert self._client is not None
        rows = self._client.all_orders(symbol=symbol)
        out: list[OpenOrder] = []
        for row in rows:
            status = str(row.get("status", ""))
            st = status.lower()
            if st in {"filled", "canceled", "cancelled", "expired", "2", "3", "4"}:
                continue
            out.append(self._normalize_open_order(row))
        return out

    def fetch_order(self, order_id: str, symbol: str | None = None) -> OpenOrder:
        self._ensure_client()
        assert self._client is not None
        detail = self._client.order_detail(order_id=order_id)
        return self._normalize_open_order(detail)

    def fetch_my_trades(
        self,
        symbol: str | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Sequence[Trade]:
        self._ensure_client()
        assert self._client is not None
        rows = self._client.my_trades(symbol=symbol)
        out: list[Trade] = []
        for row in rows:
            out.append(
                Trade(
                    exchange=self.exchange_id,
                    id=str(row.get("id") or row.get("matchId") or row.get("tradeId") or ""),
                    order_id=str(row.get("orderId") or "") or None,
                    symbol=str(row.get("symbol") or symbol or ""),
                    side=self._side_label(row.get("side")),
                    price=_to_float(row.get("price")),
                    amount=_to_float(row.get("qty") or row.get("quantity") or row.get("executedQty")),
                    cost=_to_float(row.get("quoteQty") or row.get("executedQuoteQty")),
                    fee_cost=_to_float(row.get("commission") or row.get("taxFee")),
                    fee_currency=str(row.get("commissionAsset") or row.get("taxFeeAsset") or "") or None,
                    timestamp_ms=int(row["time"]) if row.get("time") is not None else (
                        int(row["createTime"]) if row.get("createTime") is not None else None
                    ),
                )
            )
        if limit is not None:
            out = out[: int(limit)]
        return out

    def fetch_positions(self) -> Sequence[Position]:
        return []

    def _do_create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float | None,
        params: dict[str, object] | None,
    ) -> dict[str, object]:
        if self._sandbox:
            raise TradingDisabledError(
                "Tokocrypto has no native sandbox; DEMO mode must use internal paper simulator, "
                "not production order endpoints",
                exchange_id=self.exchange_id,
            )
        self._ensure_client()
        assert self._client is not None
        params = dict(params or {})
        market = self._markets.get(symbol) or self._markets.get(symbol.replace("/", "_"))
        amount_d = Decimal(str(amount))
        price_d = Decimal(str(price)) if price is not None else None
        if market is not None:
            if market.active is False:
                raise ExchangeError(f"symbol {symbol} is not active", exchange_id=self.exchange_id)
            step = None
            if market.amount_precision is not None and market.amount_precision >= 0:
                step = Decimal(1) / (Decimal(10) ** market.amount_precision)
            amount_d = _quantize(amount_d, step)
            if market.minimum_amount is not None and amount_d < Decimal(str(market.minimum_amount)):
                raise ExchangeError(
                    f"amount {amount_d} below minimum_amount {market.minimum_amount}",
                    exchange_id=self.exchange_id,
                )
            if (
                price_d is not None
                and market.minimum_cost is not None
                and (amount_d * price_d) < Decimal(str(market.minimum_cost))
            ):
                raise ExchangeError(
                    f"notional {amount_d * price_d} below minimum_cost {market.minimum_cost}",
                    exchange_id=self.exchange_id,
                )
            if price_d is not None and market.price_precision is not None and market.price_precision >= 0:
                pstep = Decimal(1) / (Decimal(10) ** market.price_precision)
                price_d = _quantize(price_d, pstep)
        toko_side = map_side(side)
        toko_type = map_order_type(order_type)
        client_id = params.get("clientOrderId") or params.get("clientId")
        client_id_s = str(client_id) if client_id is not None else None
        qty_s = decimal_str(amount_d)
        price_s = decimal_str(price_d) if price_d is not None else None
        if toko_type == TYPE_LIMIT and price_s is None:
            raise ExchangeError("limit order requires price", exchange_id=self.exchange_id)
        if toko_type == TYPE_MARKET and qty_s is None:
            raise ExchangeError("market order requires quantity", exchange_id=self.exchange_id)
        tif = TIME_IN_FORCE_GTC if toko_type == TYPE_LIMIT else None
        result = self._client.new_order(
            symbol=symbol if "_" in symbol else symbol.replace("/", "_"),
            side=toko_side,
            order_type=toko_type,
            quantity=qty_s,
            price=price_s,
            client_id=client_id_s,
            time_in_force=tif,
        )
        if result.unknown_outcome:
            return {
                "id": None,
                "clientOrderId": client_id_s,
                "symbol": symbol,
                "status": "unknown",
                "info": result.raw,
                "unknown": True,
            }
        data = result.data if isinstance(result.data, dict) else {}
        return self._normalize_order_dict(data, fallback_client_id=client_id_s, symbol=symbol)

    def _do_cancel_order(self, order_id: str, symbol: str | None) -> dict[str, object]:
        if self._sandbox:
            raise TradingDisabledError(
                "Tokocrypto DEMO mode cannot cancel production orders",
                exchange_id=self.exchange_id,
            )
        self._ensure_client()
        assert self._client is not None
        data = self._client.cancel_order(order_id=order_id)
        return self._normalize_order_dict(data, symbol=symbol or "")

    def _load_credentials(self) -> Any:
        try:
            return self._store.get("tokocrypto", self._account_id)
        except Exception as exc:
            raise CredentialMissingError(
                f"no credentials for tokocrypto/{self._account_id}",
                exchange_id=self.exchange_id,
            ) from exc

    def _ensure_client(self) -> None:
        if self._client is None:
            self.connect()

    def _load_markets(self) -> None:
        assert self._client is not None
        rows = self._client.symbols()
        markets: dict[str, Market] = {}
        raw_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or row.get("symbolName") or "")
            if not symbol:
                continue
            base = str(row.get("baseAsset") or row.get("base") or "")
            quote = str(row.get("quoteAsset") or row.get("quote") or "")
            if not base and "_" in symbol:
                base, _, quote = symbol.partition("_")
            active = row.get("status")
            if active is None:
                active_b = None
            elif str(active).lower() in {"trading", "1", "true", "enabled"}:
                active_b = True
            elif str(active).lower() in {"break", "0", "false", "disabled"}:
                active_b = False
            else:
                active_b = bool(active)
            filters = row.get("filters") if isinstance(row.get("filters"), list) else []
            min_amt = _to_float(row.get("minQty") or row.get("minimum_amount"))
            min_cost = _to_float(row.get("minNotional") or row.get("minimum_cost"))
            price_prec = row.get("quotePrecision") or row.get("pricePrecision")
            amt_prec = row.get("baseAssetPrecision") or row.get("quantityPrecision")
            for f in filters:
                if not isinstance(f, dict):
                    continue
                ft = str(f.get("filterType") or f.get("filter_type") or "").upper()
                if ft == "LOT_SIZE":
                    min_amt = _to_float(f.get("minQty")) or min_amt
                    step = f.get("stepSize")
                    if step is not None and amt_prec is None:
                        try:
                            s = Decimal(str(step))
                            if s > 0:
                                amt_prec = max(0, -s.as_tuple().exponent)
                        except Exception:
                            pass
                if ft in {"NOTIONAL", "MIN_NOTIONAL"}:
                    min_cost = _to_float(f.get("minNotional")) or min_cost
                if ft == "PRICE_FILTER":
                    tick = f.get("tickSize")
                    if tick is not None and price_prec is None:
                        try:
                            s = Decimal(str(tick))
                            if s > 0:
                                price_prec = max(0, -s.as_tuple().exponent)
                        except Exception:
                            pass
            m = Market(
                exchange=self.exchange_id,
                symbol=symbol,
                base_asset=base,
                quote_asset=quote,
                active=active_b,
                market_type=MarketType.SPOT,
                price_precision=int(price_prec) if price_prec is not None else None,
                amount_precision=int(amt_prec) if amt_prec is not None else None,
                minimum_amount=min_amt,
                minimum_cost=min_cost,
                maker_fee=_to_float(row.get("makerFee") or row.get("makerCommission")),
                taker_fee=_to_float(row.get("takerFee") or row.get("takerCommission")),
            )
            markets[symbol] = m
            raw_map[symbol] = row
        self._markets = markets
        self._raw_symbols = raw_map

    def _normalize_open_order(self, row: dict[str, Any]) -> OpenOrder:
        return OpenOrder(
            exchange=self.exchange_id,
            id=str(row.get("orderId") or row.get("id") or ""),
            client_order_id=str(row.get("clientId") or row.get("clientOrderId") or "") or None,
            symbol=str(row.get("symbol") or ""),
            side=self._side_label(row.get("side")),
            order_type=self._type_label(row.get("type")),
            status=str(row.get("status") if row.get("status") is not None else ""),
            price=_to_float(row.get("price")),
            amount=_to_float(row.get("origQty") or row.get("quantity") or row.get("amount")),
            filled=_to_float(row.get("executedQty") or row.get("filled")),
            remaining=_to_float(row.get("remaining") or row.get("origQty")),
            timestamp_ms=int(row["createTime"]) if row.get("createTime") is not None else None,
        )

    def _normalize_order_dict(
        self,
        data: dict[str, Any],
        *,
        fallback_client_id: str | None = None,
        symbol: str = "",
    ) -> dict[str, object]:
        oid = data.get("orderId") or data.get("id")
        return {
            "id": str(oid) if oid is not None else None,
            "clientOrderId": data.get("clientId") or data.get("clientOrderId") or fallback_client_id,
            "symbol": data.get("symbol") or symbol,
            "side": self._side_label(data.get("side")),
            "type": self._type_label(data.get("type")),
            "price": _to_float(data.get("price")),
            "amount": _to_float(data.get("origQty") or data.get("quantity")),
            "filled": _to_float(data.get("executedQty")),
            "remaining": _to_float(data.get("remaining")),
            "status": self._status_label(data.get("status")),
            "timestamp": data.get("createTime"),
            "info": data,
        }

    @staticmethod
    def _side_label(side: Any) -> str | None:
        if side is None:
            return None
        if str(side) in {"0", "buy", "BUY"}:
            return "buy"
        if str(side) in {"1", "sell", "SELL"}:
            return "sell"
        return str(side)

    @staticmethod
    def _type_label(t: Any) -> str | None:
        mapping = {
            "1": "limit",
            "2": "market",
            "3": "stop_loss",
            "4": "stop_loss_limit",
            "5": "take_profit",
            "6": "take_profit_limit",
            "7": "limit_maker",
        }
        if t is None:
            return None
        return mapping.get(str(t), str(t))

    @staticmethod
    def _status_label(status: Any) -> str:
        if status is None:
            return "unknown"
        s = str(status)
        mapping = {
            "0": "open",
            "1": "partially_filled",
            "2": "filled",
            "3": "canceled",
            "4": "rejected",
            "5": "expired",
        }
        return mapping.get(s, s.lower())
