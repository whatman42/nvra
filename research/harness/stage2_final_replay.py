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
    if isinstance(obj, (bytes, bytearray)):
        data = bytes(obj)
    elif isinstance(obj, str):
        data = obj.encode()
    else:
        data = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(data).hexdigest()


@dataclass
class FinalConfig:
    seed: int = 42
    symbol: str = "EURUSD"
    n_bars: int = 32
    quantity: float = 0.1
    price: float = 1.1
    correlation_id: str = "stage2.2"
    logical_ts: int = 1_700_000_000_000


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
    python_version: str
    platform: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def semantic_bundle(self) -> dict[str, str]:
        keys = (
            "input_hash",
            "multi_handler_hash",
            "event_stream_hash",
            "analysis_hash",
            "research_hash",
            "decision_hash",
            "risk_hash",
            "startup_hash",
            "state_hash",
            "final_result_hash",
        )
        return {k: getattr(self, k) for k in keys}


def _bars(seed: int, n: int, symbol: str) -> list[dict[str, Any]]:
    x, price, out = seed & 0xFFFFFFFF, 1.1, []
    for i in range(n):
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        price = round(price + ((x % 2001) - 1000) / 1e6, 5)
        out.append({"i": i, "symbol": symbol, "close": price, "logical_ts": 1_700_000_000_000 + i * 60_000})
    return out


def _analysis(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [b["close"] for b in bars]
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    return {
        "n": len(closes),
        "mean": round(mean, 8),
        "var": round(var, 12),
        "trend": round(closes[-1] - closes[0], 8),
        "last": closes[-1],
        "pipeline": "synthetic_internal",
    }


def _multi_handler(cfg: FinalConfig, analysis: dict[str, Any], *, order: str = "canonical") -> dict[str, Any]:
    """Production handlers with engines=None (architecture-supported DI)."""
    from god.orchestration import EventBus, Worker, CheckpointStore, ContextStore
    from god.orchestration.handlers import (
        CuriosityHandler,
        ResearchHandler,
        StrategyHandler,
        DriftRegimeHandler,
        PolicyCapitalHandler,
        RealityRCAHandler,
        ShadowHandler,
    )
    from god.orchestration.models import EventType, create_context, create_event

    handlers = [
        CuriosityHandler(),
        ResearchHandler(),
        StrategyHandler(),
        DriftRegimeHandler(),
        PolicyCapitalHandler(),
        RealityRCAHandler(),
        ShadowHandler(),
    ]
    if order == "reversed":
        handlers = list(reversed(handlers))

    bus = EventBus(maxsize=1024)
    contexts = ContextStore()
    checkpoints = CheckpointStore()
    worker = Worker(bus, contexts, checkpoints, handlers=handlers, poison_threshold=5)

    ctx = create_context(correlation_id=cfg.correlation_id, created_at="L0")
    contexts.save(ctx)
    obs = create_event(
        EventType.OBSERVATION,
        correlation_id=cfg.correlation_id,
        context_id=ctx.context_id,
        payload_ref={
            "analysis_mean": analysis["mean"],
            "symbol": cfg.symbol,
            "expected_metrics": {"trend": float(analysis["trend"]), "mean": float(analysis["mean"])},
            "noise": 0.0,
        },
        sequence=0,
    )
    bus.publish(obs)

    processed: list[str] = []
    for _ in range(80):
        ev = worker.process_one()
        if ev is None and bus.pending() == 0:
            break
        if ev is not None:
            processed.append(ev.event_type.value)
            while bus.pending() > 0:
                fe = bus.consume()
                if fe is None:
                    break
                bus.publish(fe)
                worker.process_one()

    ctx2 = contexts.get(ctx.context_id)
    return {
        "order": order,
        "processed": processed,
        "completed_nodes": list(ctx2.completed_nodes) if ctx2 else [],
        "evidence_index": dict(ctx2.evidence_index) if ctx2 else {},
        "handler_names": [
            type(h).__name__.removesuffix("Handler").lower() for h in handlers
        ],
    }


def _research_decision(analysis: dict[str, Any], cfg: FinalConfig) -> dict[str, Any]:
    research = {
        "hypothesis": "trend_follow" if analysis["trend"] >= 0 else "mean_revert",
        "confidence": round(min(1.0, abs(analysis["trend"]) * 100), 6),
        "source": "internal_synthetic",
        "fixture": True,
    }
    decision = {
        "side": "BUY" if analysis["trend"] >= 0 else "SELL",
        "size": cfg.quantity,
        "mode": "PAPER",
        "live_authorized": False,
        "research_hypothesis": research["hypothesis"],
    }
    return {"research": research, "decision": decision}


def _risk(cfg: FinalConfig, decision: dict[str, Any]) -> dict[str, Any]:
    from crypto.risk.engine import RiskEngine
    from crypto.risk.models import Side, TradeProposal
    from crypto.portfolio.models import ExposureBreakdown, PortfolioSnapshot

    port = PortfolioSnapshot(
        equity=10_000.0,
        available_balance=10_000.0,
        reserved_balance=0.0,
        holdings=(),
        positions=(),
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        fees=0.0,
        exposure=ExposureBreakdown(gross=0.0, net=0.0),
        timestamp_ms=cfg.logical_ts,
    )
    side = Side.BUY if decision["side"] == "BUY" else Side.SELL
    prop = TradeProposal(
        exchange_id="paper",
        account_id="demo",
        symbol=cfg.symbol,
        side=side,
        requested_quantity=decision["size"],
        requested_price=cfg.price,
        strategy_id="stage2.2",
        timestamp_ms=cfg.logical_ts,
    )
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
    import os

    from crypto.runtime.paths import DeployMode, PathResolver, set_resolver
    from crypto.runtime.startup import run_startup

    # Harness isolation: no broker, no long retry sleeps (Windows CI friendly).
    os.environ.setdefault("NVRA_STARTUP_FAST", "1")
    # Ensure no accidental live/exchange config leaks into composition probe.
    os.environ.pop("NVRA_EXCHANGE_ID", None)
    os.environ.pop("NVRA_LICENSE_SERVICE_URL", None)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        resolver = PathResolver(root, mode=DeployMode.DEV, data_root=root)
        set_resolver(resolver)
        result = run_startup(resolver, argv=["--paper"])
        errors = list(getattr(result.context, "errors", []) or [])
        return {
            "ok": bool(result.ok),
            "final_state": result.state.name,
            "exit_success": bool(result.ok and result.state.name in ("RUNNING", "READY")),
            "path": "crypto.runtime.startup.run_startup",
            "broker_credentials": False,
            "live": False,
            "errors": errors,
            "license_status": getattr(result.context, "license_status", ""),
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
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        metadata={
            "handler_order": handler_order,
            "startup": startup,
            "live_authorized": False,
            "duplicate_effects": 0,
            "mutate": mutate,
            "handlers": mh.get("handler_names"),
        },
    )


def run_final_n(cfg: FinalConfig, n: int = 100) -> list[FinalResult]:
    return [run_final(cfg, run_id=f"run-{i}") for i in range(n)]
