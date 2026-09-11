"""Unit + contract tests for Evidence Registry and ReviewArtifact."""

from __future__ import annotations

import tempfile

from god.evidence.models import (
    EvidenceGrade,
    ImpactDomain,
    PromotionState,
    ReviewDecision,
    ReviewerMode,
    EvidenceClaim,
    ReviewArtifact,
)
from god.evidence.registry import EvidenceRegistry


def test_review_artifact_roundtrip():
    claim = EvidenceClaim(
        domain=ImpactDomain.REPOSITORY,
        grade=EvidenceGrade.E1,
        claim="Repository structure inspected",
        evidence_refs=["tree"],
        environment="linux-ci",
    )
    art = ReviewArtifact.create(
        repository="whatman42/GOD",
        commit_sha="6aaa33be1f46b4ad8e52a8d2fa689d9da2488c7e",
        reviewer_mode=ReviewerMode.INDEPENDENT,
        decision=ReviewDecision.ACCEPT,
        claims=[claim],
        promotion_state=PromotionState.UNDER_REVIEW,
        notes="unit-test",
    )
    assert art.decision == ReviewDecision.ACCEPT
    assert art.claims[0].grade == EvidenceGrade.E1
    d = art.to_dict()
    restored = ReviewArtifact.from_dict(d)
    assert restored.artifact_id == art.artifact_id
    assert restored.decision == ReviewDecision.ACCEPT


def test_evidence_registry_persist_and_query():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        from pathlib import Path

        reg = EvidenceRegistry(Path(tmp) / "ev.db")
        claim = EvidenceClaim(
            domain=ImpactDomain.QUANT,
            grade=EvidenceGrade.E2,
            claim="Deterministic replay",
            evidence_refs=["hash"],
            environment="ci",
        )
        art = ReviewArtifact.create(
            repository="whatman42/nvra",
            commit_sha="sha1",
            reviewer_mode=ReviewerMode.INDEPENDENT,
            decision=ReviewDecision.ACCEPT_WITH_UNCERTAINTY,
            claims=[claim],
            promotion_state=PromotionState.UNDER_REVIEW,
            notes="persist",
        )
        reg.put(art)
        got = reg.get(art.artifact_id)
        assert got is not None
        assert got.decision == ReviewDecision.ACCEPT_WITH_UNCERTAINTY
        latest = reg.latest_for_commit("sha1")
        assert latest is not None
        assert latest.promotion_state == PromotionState.UNDER_REVIEW
        reg.close()


def test_evidence_registry_list_all():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        from pathlib import Path

        reg = EvidenceRegistry(Path(tmp) / "ev2.db")
        for i in range(2):
            claim = EvidenceClaim(
                domain=ImpactDomain.REPOSITORY,
                grade=EvidenceGrade.E0,
                claim=f"c{i}",
                evidence_refs=[],
                environment="test",
            )
            art = ReviewArtifact.create(
                repository="r",
                commit_sha=f"s{i}",
                reviewer_mode=ReviewerMode.INDEPENDENT,
                decision=ReviewDecision.RESEARCH_ONLY,
                claims=[claim],
                promotion_state=PromotionState.BLOCKED,
                notes=f"n{i}",
            )
            reg.put(art)
        items = list(reg.list_all())
        assert len(items) >= 2
        reg.close()
