"""Native Tokocrypto REST adapter tests — mock transport only, no real orders."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from crypto.core.credentials import ExchangeCredentials, InMemoryCredentialStore
from crypto.core.types import SecretStr
from crypto.exchanges.errors import (
    AuthenticationError,
    ExchangeError,
    RateLimitError,
    TradingDisabledError,
)
from crypto.exchanges.factory import create_exchange_adapter
from crypto.exchanges.tokocrypto import TokocryptoAdapter
from crypto.exchanges.tokocrypto_preflight import run_tokocrypto_preflight
from crypto.exchanges.tokocrypto_rest import (
    TransportResponse,
    canonical_query,
    decimal_str,
    map_order_type,
    map_side,
    redact,
    sign_payload,
)
from god.broker.modes import REAL_CONFIRMATION, BrokerMode


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.handlers: dict[tuple[str, str], Any] = {}
        self.default = TransportResponse(
            200, {}, json.dumps({"code": 0, "msg": "success", "data": {}, "timestamp": 1})
        )

    def on(self, method: str, path_substr: str, response: TransportResponse | Exception) -> None:
        self.handlers[(method.upper(), path_substr)] = response

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Any = None,
        params: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> TransportResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "params": dict(params or {}),
                "data": dict(data or {}),
            }
        )
        for (m, sub), resp in self.handlers.items():
            if m == method.upper() and sub in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return self.default


def _store() -> InMemoryCredentialStore:
    store = InMemoryCredentialStore()
    store.set(
        ExchangeCredentials(
            exchange_id="tokocrypto",
            account_id="default",
            api_key=SecretStr("test_api_key_1234567890"),
            api_secret=SecretStr("test_api_secret_abcdefghijklmnop"),
        )
    )
    return store


def _adapter(transport: FakeTransport, *, sandbox: bool = True) -> TokocryptoAdapter:
    return TokocryptoAdapter(
        _store(),
        sandbox=sandbox,
        transport=transport,
        clock_ms=lambda: 1_700_000_000_000,
    )


def _json(data: Any, code: int = 0, status: int = 200) -> TransportResponse:
    return TransportResponse(
        status,
        {"content-type": "application/json"},
        json.dumps({"code": code, "msg": "success", "data": data, "timestamp": 1}),
    )


def test_signature_hmac_matches_manual() -> None:
    secret = "secret"
    total = "symbol=BTC_USDT&side=0&type=1&timestamp=123"
    expected = hmac.new(secret.encode(), total.encode(), hashlib.sha256).hexdigest()
    assert sign_payload(secret, total) == expected


def test_canonical_query_sorted() -> None:
    assert canonical_query({"b": "2", "a": "1"}) == "a=1&b=2"


def test_decimal_str_no_scientific() -> None:
    assert "e" not in decimal_str("0.00000010").lower()


def test_map_side_and_type() -> None:
    assert map_side("buy") == 0
    assert map_side("SELL") == 1
    assert map_order_type("limit") == 1
    assert map_order_type("market") == 2
    with pytest.raises(ExchangeError):
        map_side("hold")


def test_redact_strips_secrets() -> None:
    text = "api_key=abcd1234secretXYZ signature=deadbeefcafebabe0123456789abcdef"
    out = redact(text)
    assert "abcd1234secretXYZ" not in out
    assert "********" in out


def test_factory_returns_native_adapter() -> None:
    ad = create_exchange_adapter("tokocrypto", _store(), sandbox=True)
    assert isinstance(ad, TokocryptoAdapter)
    assert ad.exchange_id == "tokocrypto"


def test_demo_create_order_blocked_even_if_trading_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVRA_REAL_TRADING_ENABLE", "true")
    monkeypatch.setenv("NVRA_REAL_TRADING_CONFIRM", REAL_CONFIRMATION)
    tr = FakeTransport()
    ad = _adapter(tr, sandbox=True)
    ad.enable_trading(True)
    with pytest.raises(TradingDisabledError, match="no native sandbox"):
        ad.create_order("BTC_USDT", "buy", "limit", 0.01, price=50000.0)
    assert not any(
        "/orders" in c["url"] and c["method"] == "POST" and "cancel" not in c["url"]
        for c in tr.calls
    )


def test_real_still_requires_enable_trading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVRA_REAL_TRADING_ENABLE", "true")
    monkeypatch.setenv("NVRA_REAL_TRADING_CONFIRM", REAL_CONFIRMATION)
    tr = FakeTransport()
    ad = _adapter(tr, sandbox=False)
    with pytest.raises(TradingDisabledError):
        ad.create_order("BTC_USDT", "buy", "limit", 0.01, price=50000.0)


def test_real_enable_trading_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVRA_REAL_TRADING_ENABLE", raising=False)
    monkeypatch.delenv("NVRA_REAL_TRADING_CONFIRM", raising=False)
    tr = FakeTransport()
    ad = _adapter(tr, sandbox=False)
    with pytest.raises(Exception):
        ad.enable_trading(True)


def test_connect_account_and_permissions() -> None:
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1_700_000_000_100}))
    tr.on(
        "GET",
        "/open/v1/common/symbols",
        _json(
            [
                {
                    "symbol": "BTC_USDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
                    ],
                }
            ]
        ),
    )
    tr.on(
        "GET",
        "/open/v1/account/spot",
        _json(
            {
                "canTrade": 1,
                "canWithdraw": 0,
                "accountAssets": [{"asset": "USDT", "free": "100", "locked": "0"}],
            }
        ),
    )
    ad = _adapter(tr, sandbox=True)
    ad.connect()
    bal = ad.fetch_balance()
    assert any(b.asset == "USDT" for b in bal)
    perms = ad.validate_permissions()
    assert perms.authenticated
    assert perms.trading.name == "GRANTED"
    assert perms.withdrawal.name == "DENIED"
    markets = ad.fetch_markets()
    assert any(m.symbol == "BTC_USDT" for m in markets)


def test_http_5xx_new_order_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVRA_REAL_TRADING_ENABLE", "true")
    monkeypatch.setenv("NVRA_REAL_TRADING_CONFIRM", REAL_CONFIRMATION)
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([]))
    tr.on(
        "GET",
        "/open/v1/account/spot",
        _json({"canTrade": 1, "canWithdraw": 0, "accountAssets": []}),
    )
    tr.on("POST", "/open/v1/orders", TransportResponse(503, {}, "unavailable"))
    ad = _adapter(tr, sandbox=False)
    ad.connect()
    ad.enable_trading(True)
    raw = ad.create_order(
        "BTC_USDT", "buy", "limit", 0.01, price=100.0, params={"clientOrderId": "c1"}
    )
    assert raw.get("status") == "unknown" or raw.get("unknown") is True


def test_rate_limit_429() -> None:
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([]))
    tr.on("GET", "/open/v1/account/spot", TransportResponse(429, {}, "too many"))
    ad = _adapter(tr, sandbox=True)
    with pytest.raises(RateLimitError):
        ad.connect()


def test_auth_error_on_bad_code() -> None:
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([]))
    tr.on(
        "GET",
        "/open/v1/account/spot",
        TransportResponse(
            200,
            {},
            json.dumps({"code": -2015, "msg": "Invalid API-key, IP, or permissions", "data": None}),
        ),
    )
    ad = _adapter(tr, sandbox=True)
    with pytest.raises(AuthenticationError):
        ad.connect()


def test_signed_request_contains_api_key_header_and_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NVRA_REAL_TRADING_ENABLE", "true")
    monkeypatch.setenv("NVRA_REAL_TRADING_CONFIRM", REAL_CONFIRMATION)
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([]))
    tr.on(
        "GET",
        "/open/v1/account/spot",
        _json({"canTrade": 1, "canWithdraw": 0, "accountAssets": []}),
    )
    tr.on(
        "POST",
        "/open/v1/orders",
        _json(
            {
                "orderId": 99,
                "clientId": "cid-1",
                "symbol": "BTC_USDT",
                "side": 0,
                "type": 1,
                "status": 0,
                "price": "100",
                "origQty": "0.01",
                "executedQty": "0",
            }
        ),
    )
    ad = _adapter(tr, sandbox=False)
    ad.connect()
    ad.enable_trading(True)
    raw = ad.create_order(
        "BTC_USDT", "buy", "limit", 0.01, price=100.0, params={"clientOrderId": "cid-1"}
    )
    assert raw.get("id") == "99"
    post = [c for c in tr.calls if c["method"] == "POST" and c["url"].endswith("/open/v1/orders")][0]
    assert post["headers"].get("X-MBX-APIKEY") == "test_api_key_1234567890"
    assert "signature" in post["data"]
    assert "timestamp" in post["data"]
    assert post["data"].get("side") == "0"
    assert post["data"].get("type") == "1"


def test_preflight_default_rejects_live() -> None:
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([{"symbol": "BTC_USDT", "status": "TRADING"}]))
    tr.on(
        "GET",
        "/open/v1/account/spot",
        _json({"canTrade": 1, "canWithdraw": 0, "accountAssets": []}),
    )
    ad = _adapter(tr, sandbox=True)
    report = run_tokocrypto_preflight(ad, mode=BrokerMode.DEMO, allow_real=False)
    assert report.live_submit_allowed is False
    assert any("demo" in r or "sandbox" in r for r in report.reasons)


def test_preflight_live_requires_all_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVRA_REAL_TRADING_ENABLE", "true")
    monkeypatch.setenv("NVRA_REAL_TRADING_CONFIRM", REAL_CONFIRMATION)
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([{"symbol": "BTC_USDT", "status": "TRADING"}]))
    tr.on(
        "GET",
        "/open/v1/account/spot",
        _json({"canTrade": 1, "canWithdraw": 0, "accountAssets": []}),
    )
    ad = _adapter(tr, sandbox=False)
    report = run_tokocrypto_preflight(ad, mode=BrokerMode.REAL, allow_real=True)
    assert report.live_submit_allowed is True
    assert report.withdrawal_blocked_ok is True


def test_preflight_rejects_withdrawal_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVRA_REAL_TRADING_ENABLE", "true")
    monkeypatch.setenv("NVRA_REAL_TRADING_CONFIRM", REAL_CONFIRMATION)
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([{"symbol": "BTC_USDT", "status": "TRADING"}]))
    tr.on(
        "GET",
        "/open/v1/account/spot",
        _json({"canTrade": 1, "canWithdraw": 1, "accountAssets": []}),
    )
    ad = _adapter(tr, sandbox=False)
    report = run_tokocrypto_preflight(ad, mode=BrokerMode.REAL, allow_real=True)
    assert report.live_submit_allowed is False
    assert any("withdrawal" in r for r in report.reasons)


def test_cancel_and_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVRA_REAL_TRADING_ENABLE", "true")
    monkeypatch.setenv("NVRA_REAL_TRADING_CONFIRM", REAL_CONFIRMATION)
    tr = FakeTransport()
    tr.on("GET", "/open/v1/common/time", _json({"serverTime": 1}))
    tr.on("GET", "/open/v1/common/symbols", _json([]))
    tr.on(
        "GET",
        "/open/v1/account/spot",
        _json({"canTrade": 1, "canWithdraw": 0, "accountAssets": []}),
    )
    tr.on(
        "GET",
        "/open/v1/orders/detail",
        _json({"orderId": "7", "symbol": "BTC_USDT", "status": 0, "side": 0, "type": 1}),
    )
    tr.on("POST", "/open/v1/orders/cancel", _json({"orderId": "7", "status": 3}))
    ad = _adapter(tr, sandbox=False)
    ad.connect()
    od = ad.fetch_order("7")
    assert od.id == "7"
    ad.enable_trading(True)
    cancelled = ad.cancel_order("7")
    assert cancelled.get("id") == "7"
