"""Native Tokocrypto REST transport (HMAC-SHA256). Production-only venue."""
from __future__ import annotations
import hashlib, hmac, json, logging, time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlencode
import httpx
from crypto.exchanges.errors import (
    AuthenticationError, ExchangeError, ExchangeUnavailableError,
    NetworkError, PermissionError, RateLimitError,
)
logger = logging.getLogger(__name__)
DEFAULT_BASE_URL = "https://www.tokocrypto.com"
DEFAULT_RECV_WINDOW_MS = 5000
MAX_RECV_WINDOW_MS = 60000
DEFAULT_TIMEOUT_S = 15.0
MAX_RETRIES_IDEMPOTENT = 2
RETRY_BACKOFF_S = 0.5
SIDE_BUY, SIDE_SELL = 0, 1
TYPE_LIMIT, TYPE_MARKET = 1, 2
TYPE_STOP_LOSS, TYPE_STOP_LOSS_LIMIT = 3, 4
TYPE_TAKE_PROFIT, TYPE_TAKE_PROFIT_LIMIT = 5, 6
TYPE_LIMIT_MAKER = 7
TIME_IN_FORCE_GTC = 1
_NVRA_SIDE = {"buy": SIDE_BUY, "sell": SIDE_SELL}
_NVRA_TYPE = {
    "limit": TYPE_LIMIT, "market": TYPE_MARKET, "stop_loss": TYPE_STOP_LOSS,
    "stop_loss_limit": TYPE_STOP_LOSS_LIMIT, "take_profit": TYPE_TAKE_PROFIT,
    "take_profit_limit": TYPE_TAKE_PROFIT_LIMIT, "limit_maker": TYPE_LIMIT_MAKER,
}

class Transport(Protocol):
    def request(self, method: str, url: str, *, headers=None, params=None, data=None, timeout=None) -> "TransportResponse": ...

@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    text: str
    elapsed_ms: int | None = None

class HttpxTransport:
    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)
    def request(self, method, url, *, headers=None, params=None, data=None, timeout=None):
        t0 = time.monotonic()
        try:
            resp = self._client.request(method, url, headers=dict(headers or {}),
                params=dict(params or {}) or None, data=dict(data or {}) or None,
                timeout=timeout if timeout is not None else self._timeout)
        except httpx.TimeoutException as exc:
            raise NetworkError(f"tokocrypto timeout: {type(exc).__name__}", exchange_id="tokocrypto") from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"tokocrypto network error: {type(exc).__name__}", exchange_id="tokocrypto") from exc
        return TransportResponse(resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.text, int((time.monotonic()-t0)*1000))
    def close(self):
        self._client.close()

def decimal_str(value):
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    s = format(d, "f")
    if "." in s: s = s.rstrip("0").rstrip(".")
    return s or "0"

def canonical_query(params: Mapping[str, str]) -> str:
    return urlencode(sorted((str(k), str(v)) for k, v in params.items() if v is not None), doseq=False)

def sign_payload(secret: str, total_params: str) -> str:
    return hmac.new(secret.encode(), total_params.encode(), hashlib.sha256).hexdigest()

def map_side(side: str) -> int:
    key = side.strip().lower()
    if key not in _NVRA_SIDE: raise ExchangeError(f"unsupported side: {side!r}", exchange_id="tokocrypto")
    return _NVRA_SIDE[key]

def map_order_type(order_type: str) -> int:
    key = order_type.strip().lower().replace("-", "_")
    if key not in _NVRA_TYPE: raise ExchangeError(f"unsupported order type: {order_type!r}", exchange_id="tokocrypto")
    return _NVRA_TYPE[key]

def redact(text: str) -> str:
    import re
    cleaned = re.sub(r"(?i)(api[_-]?key|secret|signature|x-mbx-apikey)\s*[:=]\s*\S+", r"\1=********", text)
    cleaned = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "********", cleaned)
    cleaned = re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "********", cleaned)
    return cleaned

@dataclass(frozen=True, slots=True)
class TokocryptoApiResult:
    code: int
    msg: str
    data: Any
    timestamp: int | None
    raw: dict[str, Any]
    http_status: int
    unknown_outcome: bool = False

class TokocryptoRestClient:
    def __init__(self, api_key: str, api_secret: str, *, base_url=DEFAULT_BASE_URL,
                 recv_window_ms=DEFAULT_RECV_WINDOW_MS, transport=None, clock_ms=None):
        if not api_key or not api_secret:
            raise AuthenticationError("api_key and api_secret required", exchange_id="tokocrypto")
        if recv_window_ms <= 0 or recv_window_ms > MAX_RECV_WINDOW_MS:
            raise ExchangeError(f"recvWindow must be 1..{MAX_RECV_WINDOW_MS}", exchange_id="tokocrypto")
        self._api_key, self._api_secret = api_key, api_secret
        self._base = base_url.rstrip("/")
        self._recv_window = recv_window_ms
        self._transport = transport or HttpxTransport()
        self._owns_transport = transport is None
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._server_time_offset_ms = 0
    def close(self):
        if self._owns_transport and hasattr(self._transport, "close"):
            self._transport.close()
    def server_time(self) -> int:
        result = self._request("GET", "/open/v1/common/time", signed=False)
        data = result.data if isinstance(result.data, dict) else {}
        ts = data.get("serverTime") or data.get("time") or result.timestamp
        if ts is None: raise ExchangeError("server time missing", exchange_id="tokocrypto")
        return int(ts)
    def sync_clock(self) -> int:
        before, server, after = self._clock_ms(), self.server_time(), self._clock_ms()
        self._server_time_offset_ms = int(server) - (before + after) // 2
        return self._server_time_offset_ms
    def symbols(self):
        result = self._request("GET", "/open/v1/common/symbols", signed=False)
        data = result.data
        if isinstance(data, list): return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("list", "symbols", "symbolList"):
                val = data.get(key)
                if isinstance(val, list): return [x for x in val if isinstance(x, dict)]
        return []
    def account_spot(self):
        result = self._request("GET", "/open/v1/account/spot", signed=True)
        if not isinstance(result.data, dict):
            raise ExchangeError("account/spot non-object", exchange_id="tokocrypto")
        return result.data
    def new_order(self, *, symbol, side, order_type, quantity=None, quote_order_qty=None,
                  price=None, client_id=None, time_in_force=None, stop_price=None,
                  self_trade_prevention_mode=None):
        body = {"symbol": symbol, "side": str(side), "type": str(order_type)}
        if quantity is not None: body["quantity"] = quantity
        if quote_order_qty is not None: body["quoteOrderQty"] = quote_order_qty
        if price is not None: body["price"] = price
        if client_id is not None: body["clientId"] = client_id
        if time_in_force is not None: body["timeInForce"] = str(time_in_force)
        if stop_price is not None: body["stopPrice"] = stop_price
        if self_trade_prevention_mode is not None: body["selfTradePreventionMode"] = str(self_trade_prevention_mode)
        return self._request("POST", "/open/v1/orders", signed=True, body=body, allow_retry=False)
    def order_detail(self, *, order_id=None, client_id=None):
        params = {}
        if order_id is not None: params["orderId"] = str(order_id)
        if client_id is not None: params["clientId"] = client_id
        if not params: raise ExchangeError("orderId or clientId required", exchange_id="tokocrypto")
        result = self._request("GET", "/open/v1/orders/detail", signed=True, params=params)
        if not isinstance(result.data, dict):
            raise ExchangeError("orders/detail non-object", exchange_id="tokocrypto")
        return result.data
    def cancel_order(self, *, order_id=None, client_id=None):
        body = {}
        if order_id is not None: body["orderId"] = str(order_id)
        if client_id is not None: body["clientId"] = client_id
        if not body: raise ExchangeError("orderId or clientId required", exchange_id="tokocrypto")
        result = self._request("POST", "/open/v1/orders/cancel", signed=True, body=body, allow_retry=False)
        if not isinstance(result.data, dict):
            if result.code == 0: return {"orderId": order_id, "clientId": client_id, "status": "canceled"}
            raise ExchangeError("orders/cancel unexpected", exchange_id="tokocrypto")
        return result.data
    def all_orders(self, *, symbol=None):
        params = {"symbol": symbol} if symbol else None
        result = self._request("GET", "/open/v1/orders", signed=True, params=params)
        data = result.data
        if isinstance(data, list): return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("list", "orders"):
                val = data.get(key)
                if isinstance(val, list): return [x for x in val if isinstance(x, dict)]
        return []
    def my_trades(self, *, symbol=None):
        params = {"symbol": symbol} if symbol else None
        result = self._request("GET", "/open/v1/orders/trades", signed=True, params=params)
        data = result.data
        if isinstance(data, list): return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("list", "trades"):
                val = data.get(key)
                if isinstance(val, list): return [x for x in val if isinstance(x, dict)]
        return []
    def _now_ms(self): return int(self._clock_ms()) + int(self._server_time_offset_ms)
    def _request(self, method, path, *, signed, params=None, body=None, allow_retry=True):
        method_u = method.upper()
        url = f"{self._base}{path}"
        attempts = (MAX_RETRIES_IDEMPOTENT + 1) if (allow_retry and method_u in {"GET", "PUT", "DELETE"}) else 1
        last_err = None
        for attempt in range(attempts):
            try:
                return self._request_once(method_u, url, path, signed=signed, params=params, body=body)
            except (RateLimitError, AuthenticationError, PermissionError):
                raise
            except (NetworkError, ExchangeUnavailableError) as exc:
                last_err = exc
                if attempt + 1 >= attempts: raise
                time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
        raise last_err
    def _request_once(self, method, url, path, *, signed, params, body):
        q, b = dict(params or {}), dict(body or {})
        headers = {"Accept": "application/json", "User-Agent": "NVRA-TokocryptoNative/1.0"}
        if signed:
            headers["X-MBX-APIKEY"] = self._api_key
            q.setdefault("timestamp", str(self._now_ms()))
            q.setdefault("recvWindow", str(self._recv_window))
            if method in {"GET", "DELETE"}:
                total = canonical_query(q) + canonical_query(b)
                q["signature"] = sign_payload(self._api_secret, total)
            else:
                if "timestamp" in q and "timestamp" not in b: b["timestamp"] = q.pop("timestamp")
                if "recvWindow" in q and "recvWindow" not in b: b["recvWindow"] = q.pop("recvWindow")
                total = canonical_query(q) + canonical_query(b)
                b["signature"] = sign_payload(self._api_secret, total)
        send_params, send_body = (q or None), (b or None)
        if send_body is not None: headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            resp = self._transport.request(method, url, headers=headers, params=send_params, data=send_body)
        except NetworkError:
            raise
        except Exception as exc:
            raise NetworkError(redact(f"transport failure: {type(exc).__name__}"), exchange_id="tokocrypto") from exc
        return self._parse_response(resp, path=path, method=method)
    def _parse_response(self, resp, *, path, method):
        status, text = resp.status_code, resp.text or ""
        if status == 429: raise RateLimitError(redact(f"rate limited on {path}"), exchange_id="tokocrypto")
        if status == 418: raise RateLimitError(redact(f"IP auto-banned (418) on {path}"), exchange_id="tokocrypto")
        if status == 403: raise PermissionError(redact(f"WAF/permission denied on {path}"), exchange_id="tokocrypto")
        if status >= 500:
            logger.warning("tokocrypto HTTP %s on %s %s — UNKNOWN", status, method, path)
            return TokocryptoApiResult(-1, redact(f"http_{status}"), None, None,
                {"http_status": status, "body": redact(text[:500])}, status, unknown_outcome=True)
        if status >= 400:
            raise ExchangeError(redact(f"HTTP {status} on {path}: {text[:200]}"), exchange_id="tokocrypto")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise ExchangeError(redact(f"invalid JSON from {path}"), exchange_id="tokocrypto") from exc
        if not isinstance(payload, dict):
            raise ExchangeError(redact(f"non-object JSON from {path}"), exchange_id="tokocrypto")
        code = int(payload.get("code", -1))
        msg = str(payload.get("msg") or payload.get("message") or "")
        data, ts = payload.get("data"), payload.get("timestamp")
        ts_i = int(ts) if ts is not None else None
        if code != 0:
            low = msg.lower()
            if any(x in low for x in ("api-key", "api key", "signature", "auth", "unauthorized")):
                raise AuthenticationError(redact(msg or f"auth failed code={code}"), exchange_id="tokocrypto")
            if "permission" in low or "not allowed" in low:
                raise PermissionError(redact(msg or f"permission denied code={code}"), exchange_id="tokocrypto")
            raise ExchangeError(redact(msg or f"tokocrypto error code={code}"), exchange_id="tokocrypto")
        return TokocryptoApiResult(code, msg, data, ts_i, payload, status, False)
