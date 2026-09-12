# Broker Modes

NVRA supports two explicit modes per broker: `DEMO` and `REAL`.

| Broker | DEMO | REAL |
|---|---|---|
| Binance | native sandbox/testnet when supported | explicit gated API execution |
| Tokocrypto | **no native sandbox** — internal paper only | explicit gated native REST execution |
| INDODAX | native sandbox/testnet when supported | explicit gated API execution |
| MetaTrader 5 | must connect to a DEMO account | must connect to a LIVE account |

## Real-mode authorization

Real execution requires **all** of these conditions:

1. `mode: REAL` for the broker.
2. `allow_real: true` for the broker.
3. `NVRA_REAL_TRADING_ENABLE=true`.
4. `NVRA_REAL_TRADING_CONFIRM=I_UNDERSTAND_REAL_TRADING`.
5. Normal immutable risk controls pass.
6. Data quality and runtime health pass.

The application never promotes a DEMO session to REAL automatically.

For CCXT exchanges, DEMO requests native sandbox activation. If the exchange
or installed CCXT adapter does not provide sandbox support, the connection is
rejected rather than silently using production endpoints.

For MT5, the connected account type is verified: DEMO mode rejects LIVE
accounts and REAL mode rejects DEMO accounts.

API keys, passwords and tokens remain outside YAML and source control.
Prefer trade-only API keys with withdrawals disabled.


## Tokocrypto (no native DEMO venue)

Tokocrypto REST is production-only. NVRA therefore:

1. Keeps `tokocrypto.mode: DEMO` / `allow_real: false` by default.
2. Blocks production `POST /open/v1/orders` while `sandbox=true`.
3. Routes simulated fills through the internal `PaperBroker`.
4. Requires REAL mode + `allow_real` + dual env confirmation + ProductionGate before live submit.
5. Prefers trade-only API keys (`canWithdraw=0`).
