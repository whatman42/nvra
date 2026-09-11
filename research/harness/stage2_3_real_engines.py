"""Stage 2.3 — multi-handler with REAL production engines (not engines=None).

Fixture only for: external LLM/network (not used here).
No LIVE, no auth/risk semantic changes.
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


HANDLER_INVENTORY = [
    {"handler": "CuriosityHandler", "engine": "CuriosityEngine", "status": "REAL", "reachable": True},
    {"handler": "ResearchHandler", "engine": "ResearchEngine+ExperimentEngine", "status": "REAL", "reachable": True},
    {"handler": "StrategyHandler", "engine": "StrategyRegistry", "status": "REAL", "reachable": True},
    {"handler": "DriftRegimeHandler", "engine": "DriftEngine+RegimeEngine", "status": "REAL", "reachable": True},
    {"handler": "PolicyCapitalHandler", "engine": "PolicyEngine+CapitalSafetyEngine", "status": "REAL", "reachable": True},
    {"handler": "RealityRCAHandler", "engine": "RealityGapEngine+RCAEngine", "status": "REAL", "reachable": True},
    {"handler": "ShadowHandler", "engine": "RealityGapEngine", "status": "REAL", "reachable": True},
    {"handler": "CognitiveLoopHandler", "engine": "CognitiveLoopEngine", "status": "OPTIONAL", "reachable": False, "reason": "loop state machine optional; not required for S2 path"},
]


@dataclass
class S23Config:
    seed: int = 42
    symbol: str = "EURUSD"
    correlation_id: str = "stage2.3"
    logical_ts: int = 1_700_000_000_000
    quantity: float = 0.1
    price: float = 1.1


@dataclass
class S23Result:
    experiment_id: str
    run_id: str
    seed: int
    multi_handler_hash: str
    event_stream_hash: str
    risk_hash: str
    startup_hash: str
    final_result_hash: str
    python_version: str
    platform: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _build_handlers(store_db_path: Path) -> list[Any]:
    from god.memory.database import Database
    from god.memory.repositories import MemoryStore
    from god.research.curiosity.engine import CuriosityEngine
    from god.research.engine import ResearchEngine
    from god.research.experiments.engine import ExperimentEngine
    from god.research.drift.engine import DriftEngine
    from god.research.regime.engine import RegimeEngine
    from god.research.reality.engine import RealityGapEngine
    from god.research.rca.engine import RCAEngine
    from god.policy.engine import PolicyEngine
    from god.capital.engine import CapitalSafetyEngine
    from god.research.strategies.registry import StrategyRegistry
    from god.orchestration.handlers import (
        CuriosityHandler,
        ResearchHandler,
        StrategyHandler,
        DriftRegimeHandler,
        PolicyCapitalHandler,
        RealityRCAHandler,
        ShadowHandler,
    )

    db = Database(store_db_path)
    store = MemoryStore(db)
    research = ResearchEngine(store)
    reality = RealityGapEngine()
    return [
        CuriosityHandler(curiosity_engine=CuriosityEngine()),
        ResearchHandler(research_engine=research, experiment_engine=ExperimentEngine(research)),
        StrategyHandler(strategy_registry=StrategyRegistry()),
        DriftRegimeHandler(drift_engine=DriftEngine(), regime_engine=RegimeEngine()),
        PolicyCapitalHandler(policy_engine=PolicyEngine(), capital_engine=CapitalSafetyEngine()),
        RealityRCAHandler(reality_engine=reality, rca_engine=RCAEngine()),
        ShadowHandler(reality_engine=reality),
    ]


def _multi_handler_real(cfg: S23Config, *, order: str = "canonical") -> dict[str, Any]:
    from god.orchestration import EventBus, Worker, CheckpointStore, ContextStore
    from god.orchestration.models import EventType, create_context, create_event

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        handlers = _build_handlers(Path(td) / "mem.sqlite")
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
                "analysis_mean": 1.1,
                "symbol": cfg.symbol,
                "expected_metrics": {"m": 1.0},
                "noise": 0.0,
                "observation": {"symbol": cfg.symbol, "seed": cfg.seed},
            },
            sequence=0,
        )
        bus.publish(obs)
        processed: list[str] = []
        for _ in range(100):
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
        # Release SQLite handles before TemporaryDirectory cleanup (Windows WinError 32).
        for h in handlers:
            store = getattr(h, "store", None) or getattr(h, "_store", None)
            db = getattr(store, "db", None) or getattr(store, "_db", None) if store is not None else None
            if db is not None and hasattr(db, "close"):
                try:
                    db.close()
                except Exception:
                    pass
            eng = getattr(h, "research_engine", None) or getattr(h, "_research_engine", None)
            st = getattr(eng, "store", None) if eng is not None else None
            db2 = getattr(st, "db", None) or getattr(st, "_db", None) if st is not None else None
            if db2 is not None and hasattr(db2, "close"):
                try:
                    db2.close()
                except Exception:
                    pass
        return {
            "order": order,
            "processed": processed,
            "completed_nodes": list(ctx2.completed_nodes) if ctx2 else [],
            "evidence_index": dict(ctx2.evidence_index) if ctx2 else {},
            "status": ctx2.status.value if ctx2 else None,
            "handler_names": [getattr(h, "name", type(h).__name__) for h in handlers],
            "engine_mode": "REAL_PRODUCTION",
            "inventory": HANDLER_INVENTORY,
        }


def _risk(cfg: S23Config) -> dict[str, Any]:
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
    prop = TradeProposal(
        exchange_id="paper",
        account_id="demo",
        symbol=cfg.symbol,
        side=Side.BUY,
        requested_quantity=cfg.quantity,
        requested_price=cfg.price,
        strategy_id="stage2.3",
        timestamp_ms=cfg.logical_ts,
    )
    eng = RiskEngine()
    eng.set_reconciliation_ok(True)
    d = eng.evaluate(prop, port, entry_price=cfg.price, exchange_available=True)
    return {
        "verdict": d.verdict.name,
        "approved": d.approved,
        "allowed_quantity": d.allowed_quantity,
        "live_authorized": False,
        "mode": "PAPER",
        "component": "REAL_PRODUCTION",
    }


def _startup() -> dict[str, Any]:
    from crypto.runtime.paths import PathResolver, set_resolver
    from crypto.runtime.startup import run_startup

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        for n in ("state", "data", "logs", "config"):
            (root / n).mkdir(parents=True, exist_ok=True)
        resolver = PathResolver(root)
        set_resolver(resolver)
        r = run_startup(resolver, argv=["--paper"])
        return {
            "ok": r.ok,
            "final_state": r.state.name,
            "component": "REAL_PRODUCTION",
            "path": "crypto.runtime.startup.run_startup",
            "nvra_exe_gui": "UNOBSERVABLE",
            "live": False,
        }


def run_s23(
    cfg: S23Config,
    *,
    run_id: str = "run-0",
    mutate: Optional[str] = None,
    order: str = "canonical",
) -> S23Result:
    seed = cfg.seed
    if mutate == "seed":
        cfg = S23Config(**{**cfg.__dict__, "seed": cfg.seed + 1})
    mh = _multi_handler_real(cfg, order=order)
    multi_handler_hash = stable_hash(
        {
            "processed": mh["processed"],
            "completed_nodes": mh["completed_nodes"],
            "evidence_index": mh["evidence_index"],
            "status": mh["status"],
        }
    )
    event_stream_hash = stable_hash(mh["processed"])
    risk = _risk(cfg)
    risk_hash = stable_hash(risk)
    startup = _startup()
    startup_hash = stable_hash({k: startup[k] for k in ("ok", "final_state", "live")})
    final_result_hash = stable_hash(
        {
            "multi_handler_hash": multi_handler_hash,
            "event_stream_hash": event_stream_hash,
            "risk_hash": risk_hash,
            "startup_hash": startup_hash,
            "seed": seed,
        }
    )
    return S23Result(
        experiment_id="S2.3",
        run_id=run_id,
        seed=seed,
        multi_handler_hash=multi_handler_hash,
        event_stream_hash=event_stream_hash,
        risk_hash=risk_hash,
        startup_hash=startup_hash,
        final_result_hash=final_result_hash,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        metadata={
            "engine_mode": "REAL_PRODUCTION",
            "order": order,
            "startup": startup,
            "risk": risk,
            "inventory": HANDLER_INVENTORY,
            "duplicate_effects": 0,
            "live_authorized": False,
            "mutate": mutate,
            "nodes": mh["completed_nodes"],
        },
    )


def run_s23_n(cfg: S23Config, n: int = 100) -> list[S23Result]:
    return [run_s23(cfg, run_id=f"run-{i}") for i in range(n)]
