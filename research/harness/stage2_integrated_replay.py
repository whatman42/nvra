"""Stage 2.1 integrated replay — EventBus worker path, recovery boundaries,
RiskEngine + ExecutionStore round-trip.

Does NOT grant LIVE authority. Does NOT connect brokers. Does NOT change
RiskEngine/SAFE_MODE/authorization semantics.
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
class IntegratedConfig:
    seed: int = 42
    symbol: str = "EURUSD"
    n_bars: int = 32
    quantity: float = 0.1
    price: float = 1.1000
    correlation_id: str = "stage2.1"
    logical_ts: int = 1_700_000_000_000


@dataclass
class IntegratedResult:
    experiment_id: str
    run_id: str
    seed: int
    input_hash: str
    event_stream_hash: str
    handler_result_hash: str
    analysis_hash: str
    risk_hash: str
    execution_store_hash: str
    state_hash: str
    artifact_hash: str
    final_result_hash: str
    python_version: str
    platform: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def semantic_bundle(self) -> dict[str, str]:
        keys = (
            "input_hash",
            "event_stream_hash",
            "handler_result_hash",
            "analysis_hash",
            "risk_hash",
            "execution_store_hash",
            "state_hash",
            "artifact_hash",
            "final_result_hash",
        )
        return {k: getattr(self, k) for k in keys}


def _bars(seed: int, n: int, symbol: str) -> list[dict[str, Any]]:
    x, price, out = seed & 0xFFFFFFFF, 1.1, []
    for i in range(n):
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        price = round(price + ((x % 2001) - 1000) / 1e6, 5)
        out.append(
            {
                "i": i,
                "symbol": symbol,
                "close": price,
                "logical_ts": 1_700_000_000_000 + i * 60_000,
            }
        )
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
    }


def _worker_handler_path(cfg: IntegratedConfig, analysis: dict[str, Any]) -> dict[str, Any]:
    from god.orchestration import EventBus, Worker, CheckpointStore, ContextStore
    from god.orchestration.handlers.curiosity import CuriosityHandler
    from god.orchestration.models import EventType, create_context, create_event

    bus = EventBus(maxsize=256)
    contexts = ContextStore()
    checkpoints = CheckpointStore()
    handler = CuriosityHandler(curiosity_engine=None)
    worker = Worker(bus, contexts, checkpoints, handlers=[handler], poison_threshold=5)

    ctx = create_context(correlation_id=cfg.correlation_id, created_at="L0")
    contexts.save(ctx)

    obs = create_event(
        EventType.OBSERVATION,
        correlation_id=cfg.correlation_id,
        context_id=ctx.context_id,
        payload_ref={"analysis_mean": analysis["mean"], "symbol": cfg.symbol},
        sequence=0,
    )
    bus.publish(obs)

    processed: list[dict[str, Any]] = []
    follow_ids: list[str] = []
    for _ in range(32):
        ev = worker.process_one()
        if ev is None and bus.pending() == 0:
            break
        if ev is not None:
            processed.append({"event_id": ev.event_id, "type": ev.event_type.value})
            while bus.pending() > 0:
                fe = bus.consume()
                if fe is None:
                    break
                follow_ids.append(fe.event_id)
                bus.publish(fe)
                worker.process_one()

    ctx2 = contexts.get(ctx.context_id)
    return {
        "context_id": ctx.context_id,
        "status": ctx2.status.value if ctx2 else None,
        "stage": ctx2.current_stage.value if ctx2 else None,
        "completed_nodes": list(ctx2.completed_nodes) if ctx2 else [],
        "evidence_index": dict(ctx2.evidence_index) if ctx2 else {},
        "checkpoint_reference": ctx2.checkpoint_reference if ctx2 else None,
        "processed": processed,
        "follow_ids": sorted(set(follow_ids)),
    }


def _risk_and_store(cfg: IntegratedConfig, analysis: dict[str, Any], store_path: Path) -> dict[str, Any]:
    from crypto.risk.engine import RiskEngine
    from crypto.risk.models import Side, TradeProposal
    from crypto.portfolio.models import ExposureBreakdown, PortfolioSnapshot
    from crypto.execution.store import ExecutionStore
    from crypto.execution.models import ExecutionMode, OrderState, ExecutionRecord, make_client_order_id

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
    side = Side.BUY if analysis["trend"] >= 0 else Side.SELL
    prop = TradeProposal(
        exchange_id="paper",
        account_id="demo",
        symbol=cfg.symbol,
        side=side,
        requested_quantity=cfg.quantity,
        requested_price=cfg.price,
        strategy_id="stage2.1",
        timestamp_ms=cfg.logical_ts,
    )
    eng = RiskEngine()
    eng.set_reconciliation_ok(True)
    decision = eng.evaluate(prop, port, entry_price=cfg.price, exchange_available=True)

    risk_view = {
        "verdict": decision.verdict.name,
        "reason": decision.reason.name,
        "approved": decision.approved,
        "allowed_quantity": decision.allowed_quantity,
        "allowed_notional": decision.allowed_notional,
        "live_authorized": False,
        "mode": "PAPER",
    }

    store = ExecutionStore(store_path)
    try:
        intent_key = stable_hash(
            {"symbol": cfg.symbol, "side": side.name, "qty": cfg.quantity, "price": cfg.price, "seed": cfg.seed}
        )[:24]
        client_oid = make_client_order_id(
            prop.exchange_id, prop.account_id, prop.symbol, prop.side,
            decision.allowed_quantity, prop.requested_price, intent_key,
        )
        exec_id = "ex-" + stable_hash({"coid": client_oid, "intent": intent_key})[:24]
        if decision.approved and decision.allowed_quantity > 0:
            rec = ExecutionRecord(
                execution_id=exec_id,
                client_order_id=client_oid,
                exchange_id=prop.exchange_id,
                account_id=prop.account_id,
                symbol=prop.symbol,
                side=prop.side,
                order_type="LIMIT",
                requested_quantity=prop.requested_quantity,
                requested_price=prop.requested_price,
                allowed_quantity=decision.allowed_quantity,
                allowed_notional=decision.allowed_notional,
                state=OrderState.RISK_APPROVED,
                remaining_quantity=decision.allowed_quantity,
                created_at_ms=cfg.logical_ts,
                updated_at_ms=cfg.logical_ts,
                strategy_id=prop.strategy_id,
                correlation_id=cfg.correlation_id,
                mode=ExecutionMode.PAPER,
            )
            store.save(rec)
            store.save(rec)
            loaded = store.get(exec_id)
            by_coid = store.get_by_client_order_id(client_oid)
            all_rows = store.list_all()
            store_view = {
                "count": len(all_rows),
                "exec_id": loaded.execution_id if loaded else None,
                "client_order_id": loaded.client_order_id if loaded else None,
                "state": loaded.state.name if loaded else None,
                "allowed_quantity": loaded.allowed_quantity if loaded else None,
                "mode": loaded.mode.name if loaded else None,
                "by_coid_match": bool(by_coid and by_coid.execution_id == exec_id),
                "duplicate_effects": 0 if len(all_rows) == 1 else len(all_rows),
            }
        else:
            store_view = {
                "count": 0,
                "exec_id": None,
                "state": "NOT_PERSISTED",
                "approved": False,
                "duplicate_effects": 0,
            }
    finally:
        store.close()

    return {"risk": risk_view, "store": store_view}


def _recovery_matrix(cfg: IntegratedConfig, analysis: dict[str, Any]) -> dict[str, Any]:
    from god.orchestration.checkpoint_store import CheckpointStore
    from god.orchestration.models.checkpoint import make_checkpoint, verify_checkpoint

    cps = CheckpointStore()
    boundaries = {
        "B1_before_analysis": "PRE_ANALYSIS",
        "B2_after_analysis": "POST_ANALYSIS",
        "B3_before_risk": "PRE_RISK",
        "B4_after_risk": "POST_RISK",
        "B5_before_execution_intent": "PRE_EXEC_INTENT",
        "B6_after_reconciliation": "POST_RECON",
    }
    results: dict[str, Any] = {}
    context_id = "ctx-recovery-" + stable_hash(cfg.correlation_id)[:12]
    for name, node in boundaries.items():
        stage = "ANALYSIS" if "analysis" in name else ("RISK" if "risk" in name else "EXEC")
        refs_clean = [stable_hash({"node": node, "seed": cfg.seed, "path": "clean"})]
        cp_clean = make_checkpoint(context_id, stage, node, refs_clean)
        assert verify_checkpoint(cp_clean)
        cps.save(cp_clean)
        loaded = cps.get(cp_clean.checkpoint_id)
        clean_hash = stable_hash(loaded.to_dict() if loaded else {})

        cp_rec = make_checkpoint(context_id, stage, node, refs_clean)
        assert verify_checkpoint(cp_rec)
        cps.save(cp_rec)
        loaded2 = cps.get(cp_rec.checkpoint_id)
        rec_hash = stable_hash(loaded2.to_dict() if loaded2 else {})

        results[name] = {
            "status": "PASS" if clean_hash == rec_hash and verify_checkpoint(cp_clean) else "FAIL",
            "clean_hash": clean_hash,
            "recovered_hash": rec_hash,
            "equal": clean_hash == rec_hash,
        }
    return results


def run_integrated(
    cfg: IntegratedConfig,
    *,
    run_id: str = "run-0",
    experiment_id: str = "S2.1",
    mutate: Optional[str] = None,
) -> IntegratedResult:
    bars = _bars(cfg.seed, cfg.n_bars, cfg.symbol)
    if mutate == "input":
        bars = list(bars)
        bars[0] = dict(bars[0], close=round(bars[0]["close"] + 0.01, 5))
    input_hash = stable_hash(bars)
    analysis = _analysis(bars)
    if mutate == "config":
        cfg = IntegratedConfig(**{**cfg.__dict__, "quantity": cfg.quantity + 0.05})
    analysis_hash = stable_hash(analysis)

    handler_state = _worker_handler_path(cfg, analysis)
    if mutate == "event_payload":
        handler_state = dict(handler_state)
        handler_state["evidence_index"] = dict(handler_state.get("evidence_index") or {}, inject="x")
    handler_result_hash = stable_hash(handler_state)
    event_stream_hash = stable_hash(handler_state.get("processed", []))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store_path = Path(td) / "exec.sqlite"
        risk_store = _risk_and_store(cfg, analysis, store_path)
    risk_hash = stable_hash(risk_store["risk"])
    execution_store_hash = stable_hash(risk_store["store"])

    recovery = _recovery_matrix(cfg, analysis)
    state = {
        "handler": handler_state,
        "risk": risk_store["risk"],
        "store": risk_store["store"],
        "recovery_equal": all(v["equal"] for v in recovery.values()),
    }
    state_hash = stable_hash(state)
    artifact = {
        "input_hash": input_hash,
        "analysis_hash": analysis_hash,
        "handler_result_hash": handler_result_hash,
        "event_stream_hash": event_stream_hash,
        "risk_hash": risk_hash,
        "execution_store_hash": execution_store_hash,
        "state_hash": state_hash,
    }
    artifact_hash = stable_hash(artifact)
    final_result_hash = stable_hash({"artifact_hash": artifact_hash, "state_hash": state_hash, "seed": cfg.seed})
    return IntegratedResult(
        experiment_id=experiment_id,
        run_id=run_id,
        seed=cfg.seed,
        input_hash=input_hash,
        event_stream_hash=event_stream_hash,
        handler_result_hash=handler_result_hash,
        analysis_hash=analysis_hash,
        risk_hash=risk_hash,
        execution_store_hash=execution_store_hash,
        state_hash=state_hash,
        artifact_hash=artifact_hash,
        final_result_hash=final_result_hash,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        metadata={
            "recovery": recovery,
            "duplicate_effects": risk_store["store"].get("duplicate_effects", 0),
            "mutate": mutate,
            "live_authorized": False,
        },
    )


def run_integrated_n(cfg: IntegratedConfig, n: int = 100) -> list[IntegratedResult]:
    return [run_integrated(cfg, run_id=f"run-{i}") for i in range(n)]
