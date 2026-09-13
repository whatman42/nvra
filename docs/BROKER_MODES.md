# Broker Modes

NVRA supports explicit modes per broker: `DEMO`, `REAL`, and for MT5 `FOLLOW_TERMINAL`.

| Venue | Mode | Execution |
|---|---|---|
| Binance | DEMO default | sandbox/testnet when supported |
| Tokocrypto | REAL (no native DEMO) | gated native REST real capital |
| INDODAX | DEMO default | sandbox when supported |
| MetaTrader 5 | **FOLLOW_TERMINAL** | follows account logged into MT5 terminal |
| IDX (equity) | **signal only** | no order placement from NVRA |

## Real-mode authorization (crypto / explicit REAL)

Real execution requires **all** of these conditions:

1. `mode: REAL` (or FOLLOW_TERMINAL with LIVE terminal for MT5).
2. `allow_real: true` for the broker.
3. `NVRA_REAL_TRADING_ENABLE=true`.
4. `NVRA_REAL_TRADING_CONFIRM=I_UNDERSTAND_REAL_TRADING`.
5. ProductionGate passes.
6. Risk, data quality, runtime health, permission, and preflight pass.
7. Prefer trade-only API keys with withdrawals disabled (`canWithdraw=0`).

Configuration may be REAL-ready while transaction authorization remains OFF until every gate above is explicit and current.

LiveGate may auto-promote `ExecutionMode.LIVE` after checklist success when dual env is already set (one-time setup). Soft failures demote to PAPER with linear backoff; hard failures require human intervention.

## MT5 — follow terminal account

There is **no** DEMO/REAL selector in NVRA for MT5.

- Terminal account `trade_mode` DEMO → DEMO constraints only.
- Terminal account LIVE → LIVE path allowed (still subject to dual env + gates).
- Switch account inside MT5 terminal → NVRA follows on next health/reconnect check.

## IDX — signal only

IDX path produces signals and research only. `idx.signal_only: true` and `idx.execution_enabled: false` block equity order submission from NVRA.

## Notifications

All operational notifications (startup, live gate, orders, risk, health, errors, signals) route to **Telegram** when `telegram.enabled: true` and `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are set.

API keys, passwords and tokens remain outside YAML and source control.

## Tokocrypto (no native DEMO venue)

Tokocrypto REST is production-only. NVRA therefore:

1. Defaults `tokocrypto.mode: REAL` / `sandbox: false` / `allow_real: true`.
2. Blocks `POST /open/v1/orders` until `enable_trading` succeeds (dual env).
3. Requires ProductionGate + risk + data quality + health + permission + preflight.
4. Prefers trade-only API keys (`canWithdraw=0`).
