"""Stage 2.2 final integrated replay — multi-handler EventBus, startup composition,
analysis→research→decision path.

No LIVE, no broker credentials, no RiskEngine/auth semantic changes.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class FinalConfig:
    seed: int = 1
    n_bars: int = 32
    symbol: str = "NVRA"
    price: float = 100.0
    quantity: float = 1.0


@dataclass
class FinalResult:
    experiment_id: str
    run_id: str
    seed: int
    input_hash: str
    multi_handler_hash: str
    event_stream_hash: str
    analysis_hash: str
    research_hash: str
    decision_hash: str
    risk_hash: str
    startup_hash: str
    state_hash: str
    final_result_hash: str
    platform: str = field(default_factory=lambda: platform.platform())
    python: str = field(default_factory=lambda: sys.version.split()[0])
    metadata: dict[str, Any] = field(default_factory=dict)

    def semantic_bundle(self) -> dict[str, str]:
        return {
            "input_hash": self.input_hash,
            "multi_handler_hash": self.multi_handler_hash,
            "event_stream_hash": self.event_stream_hash,
            "analysis_hash": self.analysis_hash,
            "research_hash": self.research_hash,
            "decision_hash": self.decision_hash,
            "risk_hash": self.risk_hash,
            "startup_hash": self.startup_hash,
            "state_hash": self.state_hash,
            "final_result_hash": self.final_result_hash,
        }


def _bars(seed: int, n: int, symbol: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    price = 100.0 + (seed % 7) * 0.1
    for i in range(n):
        price = round(price * (1.0 + ((seed * (i + 1)) % 5 - 2) * 0.001), 5)
        out.append({"i": i, "symbol": symbol, "close": price, "logical_ts": 1_700_000_000_000 + i * 60_000})
    return out


def _analysis(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [b["close"] for b in bars]
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    return {
        "n": len(closes),
        "mean": round(mean, 8),
        "var": round(var, 8),
        "trend": round(closes[-1] - closes[0], 8),
        "last": closes[-1],
    }


def _multi_handler(cfg: FinalConfig, analysis: dict[str, Any], order: str = "canonical") -> dict[str, Any]:
    from god.core.events import Event, EventBus

    bus = EventBus()
    processed: list[dict[str, Any]] = []

    def curiosity(ev: Event) -> None:
        processed.append({"handler": "curiosity", "type": ev.type, "payload": dict(ev.payload)})

    def research(ev: Event) -> None:
        processed.append({"handler": "research", "type": ev.type, "payload": dict(ev.payload)})

    def strategy(ev: Event) -> None:
        processed.append({"handler": "strategy", "type": ev.type, "payload": dict(ev.payload)})

    handlers = [
        ("curiosity", curiosity),
        ("research", research),
        ("strategy", strategy),
    ]
    if order == "reversed":
        handlers = list(reversed(handlers))

    names = [n for n, _ in handlers]
    for name, fn in handlers:
        bus.subscribe("BAR", fn)

    for bar in _bars(cfg.seed, min(8, cfg.n_bars), cfg.symbol):
        bus.publish(Event(type="BAR", payload=bar))

    return {"handlers": names, "processed": processed, "analysis_ref": analysis.get("last")}


def _research_decision(analysis: dict[str, Any], cfg: FinalConfig) -> dict[str, Any]:
    research = {
        "signal": "BUY" if analysis["trend"] >= 0 else "SELL",
        "confidence": round(min(1.0, abs(analysis["trend"]) * 10 + 0.2), 6),
        "mean": analysis["mean"],
    }
    decision = {
        "action": research["signal"],
        "symbol": cfg.symbol,
        "quantity": cfg.quantity,
        "price": cfg.price,
        "confidence": research["confidence"],
    }
    return {"research": research, "decision": decision}


def _risk(cfg: FinalConfig, decision: dict[str, Any]) -> dict[str, Any]:
    from god.risk.engine import PortfolioState, Proposal, RiskEngine

    prop = Proposal(
        symbol=str(decision["symbol"]),
        side=str(decision["action"]),
        quantity=float(decision["quantity"]),
        price=float(decision.get("price") or cfg.price),
    )
    port = PortfolioState(cash=1_000_000.0, positions={})
    eng = RiskEngine()
    eng.set_reconciliation_ok(True)
    d = eng.evaluate(prop, port, entry_price=cfg.price, exchange_available=True)
    return {
        "verdict": d.verdict.name,
        "reason": d.reason.name,
        "approved": d.approved,
        "allowed_quantity": d.allowed_quantity,
        "live_authorized": False,
        "mode": "PAPER",
    }


def _startup_composition() -> dict[str, Any]:
    from crypto.runtime.paths import PathResolver, set_resolver
    from crypto.runtime.startup import run_startup

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        for name in ("state", "data", "logs", "config"):
            (root / name).mkdir(parents=True, exist_ok=True)
        resolver = PathResolver(root)
        set_resolver(resolver)
        result = run_startup(resolver, argv=["--paper"])
        return {
            "ok": result.ok,
            "final_state": result.state.name,
            "exit_success": bool(result.ok and result.state.name in ("RUNNING", "READY")),
            "path": "crypto.runtime.startup.run_startup",
            "broker_credentials": False,
            "live": False,
        }


def run_final(
    cfg: FinalConfig,
    *,
    run_id: str = "run-0",
    mutate: Optional[str] = None,
    handler_order: str = "canonical",
) -> FinalResult:
    bars = _bars(cfg.seed, cfg.n_bars, cfg.symbol)
    if mutate == "input":
        bars = list(bars)
        bars[0] = dict(bars[0], close=round(bars[0]["close"] + 0.01, 5))
    input_hash = stable_hash(bars)
    analysis = _analysis(bars)
    analysis_hash = stable_hash(analysis)

    mh = _multi_handler(cfg, analysis, order=handler_order)
    multi_handler_hash = stable_hash(mh)
    event_stream_hash = stable_hash(mh["processed"])

    rd = _research_decision(analysis, cfg)
    research_hash = stable_hash(rd["research"])
    decision_hash = stable_hash(rd["decision"])
    risk = _risk(cfg, rd["decision"])
    risk_hash = stable_hash(risk)

    startup = _startup_composition()
    startup_hash = stable_hash(startup)

    state = {
        "multi_handler": mh,
        "research": rd["research"],
        "decision": rd["decision"],
        "risk": risk,
        "startup": startup,
    }
    state_hash = stable_hash(state)
    final_result_hash = stable_hash(
        {
            "input_hash": input_hash,
            "multi_handler_hash": multi_handler_hash,
            "analysis_hash": analysis_hash,
            "research_hash": research_hash,
            "decision_hash": decision_hash,
            "risk_hash": risk_hash,
            "startup_hash": startup_hash,
            "state_hash": state_hash,
            "seed": cfg.seed,
        }
    )
    return FinalResult(
        experiment_id="S2.2",
        run_id=run_id,
        seed=cfg.seed,
        input_hash=input_hash,
        multi_handler_hash=multi_handler_hash,
        event_stream_hash=event_stream_hash,
        analysis_hash=analysis_hash,
        research_hash=research_hash,
        decision_hash=decision_hash,
        risk_hash=risk_hash,
        startup_hash=startup_hash,
        state_hash=state_hash,
        final_result_hash=final_result_hash,
        metadata={
            "handlers": mh["handlers"],
            "startup": startup,
            "live_authorized": False,
            "risk": risk,
        },
    )


def run_final_n(cfg: FinalConfig, n: int) -> list[FinalResult]:
    return [run_final(cfg, run_id=f"run-{i}") for i in range(n)]
