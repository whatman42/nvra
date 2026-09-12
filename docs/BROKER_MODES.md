# Broker Modes

NVRA supports two explicit modes per broker: `DEMO` and `REAL`.

| Broker | DEMO | REAL |
|---|---|---|
| Binance | native sandbox/testnet when supported | explicit gated API execution |
| Tokocrypto | **no native sandbox** — internal paper only | explicit gated native REST execution (default profile) |
| INDODAX | native sandbox/testnet when supported | explicit gated API execution |
| MetaTrader 5 | must connect to a DEMO account | must connect to a LIVE account |

## Real-mode authorization

Real execution requires **all** of these conditions:

1. `mode: REAL` for the broker.
2. `allow_real: true` for the broker.
3. `NVRA_REAL_TRADING_ENABLE=true`.
4. `NVRA_REAL_TRADING_CONFIRM=I_UNDERSTAND_REAL_TRADING`.
5. ProductionGate passes.
6. Risk, data quality, runtime health, permission, and preflight pass.
7. Prefer trade-only API keys with withdrawals disabled (`canWithdraw=0`).

The application never places real orders automatically. Configuration may be
REAL-ready while transaction authorization remains OFF until every gate above
is explicit and current.

The application never promotes a DEMO session to REAL automatically.

For CCXT exchanges, DEMO requests native sandbox activation. If the exchange
or installed CCXT adapter does not provide sandbox support, the connection is
rejected rather than silently using production endpoints.

For MT5, the connected account type is verified: DEMO mode rejects LIVE
accounts and REAL mode rejects DEMO accounts.

API keys, passwords and tokens remain outside YAML and source control.


## Tokocrypto (no native DEMO venue)

Tokocrypto REST is production-only. NVRA therefore:

1. Defaults `tokocrypto.mode: REAL` / `sandbox: false` / `allow_real: true`
   (live-ready profile — option A).
2. Still blocks `POST /open/v1/orders` until `enable_trading` succeeds, which
   requires dual env confirmation (`NVRA_REAL_TRADING_ENABLE` + exact phrase).
3. Requires ProductionGate + risk + data quality + health + permission +
   preflight before live submit.
4. If an operator deliberately selects DEMO, routes simulated fills through
   the internal `PaperBroker` and never hits production order endpoints.
5. Prefers trade-only API keys (`canWithdraw=0`); preflight rejects withdrawal-enabled keys for live.
