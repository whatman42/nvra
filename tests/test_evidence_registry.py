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
        impact_classification=[ImpactDomain.REPOSITORY, ImpactDomain.MEMORY],
        evidence_claims=[claim],
        decision=ReviewDecision.RESEARCH_ONLY,
        promotion_state=PromotionState.RESEARCH_ONLY,
        remaining_uncertainty="No real Windows/MT evidence",
        change_summary="Initial Evidence Registry introduction",
    )
    d = art.to_dict()
    art2 = ReviewArtifact.from_dict(d)
    assert art2.artifact_id == art.artifact_id
    assert art2.commit_sha == art.commit_sha
    assert art2.decision == ReviewDecision.RESEARCH_ONLY
    assert len(art2.evidence_claims) == 1
    assert art2.evidence_claims[0].grade == EvidenceGrade.E1


def test_registry_append_and_get():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        reg = EvidenceRegistry(root=tmp)
        claim = EvidenceClaim(
            domain=ImpactDomain.ACCOUNTING,
            grade=EvidenceGrade.E2,
            claim="Virtual PnL carries fees",
            evidence_refs=["tests/test_agent.py"],
            environment="linux-ci",
        )
        art = ReviewArtifact.create(
            repository="whatman42/GOD",
            commit_sha="abc123",
            reviewer_mode=ReviewerMode.INDEPENDENT,
            impact_classification=[ImpactDomain.ACCOUNTING],
            evidence_claims=[claim],
            decision=ReviewDecision.ACCEPT_WITH_UNCERTAINTY,
            promotion_state=PromotionState.UNDER_REVIEW,
            remaining_uncertainty="Real broker costs unknown",
        )
        aid = reg.append(art)
        assert aid == art.artifact_id
        loaded = reg.get(aid)
        assert loaded is not None
        assert loaded.decision == ReviewDecision.ACCEPT_WITH_UNCERTAINTY
        assert loaded.promotion_state == PromotionState.UNDER_REVIEW


def test_registry_list_and_latest():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        reg = EvidenceRegistry(root=tmp)
        for i, state in enumerate([PromotionState.RESEARCH_ONLY, PromotionState.UNDER_REVIEW]):
            art = ReviewArtifact.create(
                repository="whatman42/GOD",
                commit_sha=f"sha{i}",
                reviewer_mode=ReviewerMode.INDEPENDENT,
                impact_classification=[ImpactDomain.RESEARCH],
                evidence_claims=[],
                decision=ReviewDecision.RESEARCH_ONLY,
                promotion_state=state,
                remaining_uncertainty="test",
            )
            reg.append(art)
        recent = reg.list_recent(10)
        assert len(recent) == 2
        latest = reg.latest_for_commit("sha1")
        assert latest is not None
        assert latest.promotion_state == PromotionState.UNDER_REVIEW
