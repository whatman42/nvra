"""Stage 2.2 final integrated multi-handler + startup composition qualification."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from research.harness.stage2_final_replay import FinalConfig, run_final, run_final_n


def test_final_deterministic_pair():
    cfg = FinalConfig(seed=7)
    a = run_final(cfg, run_id="a")
    b = run_final(cfg, run_id="b")
    assert a.semantic_bundle() == b.semantic_bundle()


def test_final_100_runs():
    cfg = FinalConfig(seed=11)
    results = run_final_n(cfg, 100)
    assert len(results) == 100
    assert len({r.final_result_hash for r in results}) == 1


def test_startup_composition_running():
    r = run_final(FinalConfig(seed=3))
    st = r.metadata["startup"]
    assert st["ok"] is True, (
        f"startup failed state={st.get('final_state')} "
        f"license={st.get('license_status')} errors={st.get('errors')}"
    )
    assert st["final_state"] in ("RUNNING", "READY")
    assert st["broker_credentials"] is False
    assert st["live"] is False
    hashes = {run_final(FinalConfig(seed=3), run_id=str(i)).startup_hash for i in range(20)}
    assert len(hashes) == 1


def test_handler_order_semantically_dependent():
    cfg = FinalConfig(seed=5)
    a = run_final(cfg, handler_order="canonical")
    b = run_final(cfg, handler_order="reversed")
    assert a.multi_handler_hash != b.multi_handler_hash


def test_divergence_input():
    cfg = FinalConfig(seed=9)
    base = run_final(cfg)
    mut = run_final(cfg, mutate="input")
    assert base.final_result_hash != mut.final_result_hash


def test_analysis_research_decision_chain():
    r = run_final(FinalConfig(seed=2))
    assert r.analysis_hash
    assert r.research_hash
    assert r.decision_hash
    assert r.risk_hash
    assert r.metadata.get("live_authorized") is False


def test_multi_handler_names_present():
    r = run_final(FinalConfig(seed=4))
    names = r.metadata.get("handlers") or []
    assert "curiosity" in names
    assert "research" in names
    assert "strategy" in names
