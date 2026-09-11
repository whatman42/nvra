"""Unit + contract tests for Evidence Registry and ReviewArtifact.

Windows-safe TemporaryDirectory cleanup after SQLite usage.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from god.evidence.registry import EvidenceRegistry, ReviewArtifact


def test_evidence_registry_roundtrip():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "ev.db"
        reg = EvidenceRegistry(db_path=db)
        art = ReviewArtifact.create(
            subject="test-subject",
            verdict="ACCEPT",
            grade="E3",
            notes="roundtrip",
        )
        reg.put(art)
        loaded = reg.get(art.artifact_id)
        assert loaded is not None
        assert loaded.verdict == "ACCEPT"
        reg.close()


def test_evidence_registry_list():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "ev2.db"
        reg = EvidenceRegistry(db_path=db)
        for i in range(3):
            reg.put(
                ReviewArtifact.create(
                    subject=f"s{i}",
                    verdict="ACCEPT",
                    grade="E2",
                    notes=f"n{i}",
                )
            )
        items = reg.list_all()
        assert len(items) >= 3
        reg.close()
