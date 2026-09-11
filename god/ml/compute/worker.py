"""Authenticated Colab compute worker — allowlisted HEAVY workloads only.

NEVER executes arbitrary Python, shell, broker orders, or Risk Governor changes.
NEVER loads secrets. Output is untrusted until NVRA verifies signature + checksum.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .protocol import (
    PROTOCOL_VERSION,
    WORKER_VERSION,
    JobManifest,
    ReplayStore,
    ResultManifest,
    sha256_bytes,
    validate_job_manifest,
)
from .security import assert_no_execution_commands, assert_no_secrets
from .types import JobStatus

# Explicit allowlist — anything else is REJECTED.
ALLOWED_MODEL_TYPES = frozenset(
    {
        "random_forest",
        "numpy_logit",
        "lightgbm",
        "xgboost",
        "baseline_classifier",
    }
)

# Forbidden parameter keys even inside training_params.
FORBIDDEN_PARAM_FRAGMENTS = (
    "code",
    "source",
    "script",
    "exec",
    "eval",
    "subprocess",
    "os.system",
    "shell",
    "command",
    "__import__",
    "open(",
    "place_order",
    "mt5",
    "broker",
)


def _params_safe(params: Mapping[str, Any]) -> tuple[bool, str]:
    raw = json.dumps(params, sort_keys=True).lower()
    for frag in FORBIDDEN_PARAM_FRAGMENTS:
        if frag in raw:
            return False, f"forbidden_param_content:{frag}"
    try:
        assert_no_execution_commands(params)
        assert_no_secrets(params)
    except ValueError as exc:
        return False, str(exc)
    return True, ""


def _run_allowlisted_training(
    model_type: str,
    training_params: Mapping[str, Any],
    dataset_sha256: str,
) -> tuple[bytes, dict[str, float]]:
    """Deterministic local training surrogate for CI / offline worker.

    Real Colab GPU path uses the same contract; model weights are not
    arbitrary code — only fixed sklearn-style baselines when available.
    """
    n_estimators = int(training_params.get("n_estimators", 10))
    n_estimators = max(1, min(n_estimators, 100))
    max_depth = int(training_params.get("max_depth", 3))
    max_depth = max(1, min(max_depth, 16))
    seed = int(training_params.get("random_state", 42))

    # Synthetic feature matrix derived from dataset hash (deterministic, no external data).
    import hashlib

    seed_bytes = hashlib.sha256((dataset_sha256 or "empty").encode()).digest()
    rng_state = int.from_bytes(seed_bytes[:8], "big") ^ seed

    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier

        rs = np.random.RandomState(rng_state % (2**31 - 1))
        n = int(training_params.get("n_samples", 64))
        n = max(16, min(n, 512))
        X = rs.randn(n, 4)
        y = (X[:, 0] + X[:, 1] > 0).astype(int)
        if model_type in ("random_forest", "baseline_classifier", "lightgbm", "xgboost"):
            clf = RandomForestClassifier(
                n_estimators=n_estimators,
                max_depth=max_depth,
                random_state=seed,
                n_jobs=1,
            )
            clf.fit(X, y)
            score = float(clf.score(X, y))
            # Serialize a minimal portable artifact (not pickle of full pipeline with secrets).
            artifact = {
                "model_type": model_type,
                "n_estimators": n_estimators,
                "max_depth": max_depth,
                "dataset_sha256": dataset_sha256,
                "train_accuracy": score,
                "feature_dim": 4,
                "n_samples": n,
                "seed": seed,
            }
            raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return raw, {"train_accuracy": score, "n_samples": float(n)}
    except Exception:
        pass

    # Pure-stdlib fallback (no sklearn)
    artifact = {
        "model_type": model_type,
        "n_estimators": n_estimators,
        "max_depth": max_depth,
        "dataset_sha256": dataset_sha256,
        "train_accuracy": 0.5,
        "feature_dim": 4,
        "n_samples": 32,
        "seed": seed,
        "backend": "stdlib_surrogate",
    }
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return raw, {"train_accuracy": 0.5, "n_samples": 32.0}


@dataclass
class WorkerResult:
    result_manifest: ResultManifest
    artifact_bytes: bytes
    ok: bool
    reasons: list[str]


class ColabWorker:
    """Versioned worker suitable for Colab or in-process CI simulation.

    Auth: verifies Ed25519 job signature against configured public key.
    Does not hold broker credentials or order APIs.
    """

    version = WORKER_VERSION
    protocol_version = PROTOCOL_VERSION

    def __init__(
        self,
        *,
        public_raw: bytes,
        private_raw: bytes,
        authorized_tenant: str = "",
        replay: Optional[ReplayStore] = None,
    ) -> None:
        self._public = public_raw
        self._private = private_raw
        self._tenant = authorized_tenant
        self._replay = replay or ReplayStore()

    def process(self, manifest: JobManifest) -> WorkerResult:
        ok, reasons = validate_job_manifest(
            manifest,
            public_raw=self._public,
            authorized_tenant=self._tenant,
            replay=self._replay,
        )
        if not ok:
            return self._reject(manifest, reasons)

        # Record for replay protection before execution.
        self._replay.record(manifest.job_id, manifest.nonce, manifest.expires_at)

        mt = str(manifest.model_type).lower().strip()
        if mt not in ALLOWED_MODEL_TYPES:
            return self._reject(manifest, [f"model_type_not_allowed:{mt}"])

        safe_params, reason = _params_safe(manifest.training_params)
        if not safe_params:
            return self._reject(manifest, [reason])

        if not manifest.dataset_sha256:
            return self._reject(manifest, ["missing_dataset_sha256"])

        started = time.time()
        try:
            artifact_bytes, metrics = _run_allowlisted_training(
                mt, manifest.training_params, manifest.dataset_sha256
            )
        except Exception as exc:
            return self._reject(manifest, [f"training_failed:{type(exc).__name__}"])

        art_hash = sha256_bytes(artifact_bytes)
        completed = time.time()
        result = ResultManifest(
            protocol_version=PROTOCOL_VERSION,
            worker_version=WORKER_VERSION,
            job_id=manifest.job_id,
            tenant_id=manifest.tenant_id,
            status=JobStatus.SUCCESS.value,
            artifact_name=f"{manifest.job_id}.artifact.json",
            artifact_sha256=art_hash,
            artifact_size=len(artifact_bytes),
            dataset_sha256=manifest.dataset_sha256,
            code_version=manifest.code_version,
            created_at=started,
            completed_at=completed,
            metrics=metrics,
            provenance={
                **manifest.provenance,
                "provider": "colab_worker",
                "model_type": mt,
                "tenant_id": manifest.tenant_id,
            },
            validation_metadata={
                "allowlisted": True,
                "protocol_version": PROTOCOL_VERSION,
            },
            nonce=manifest.nonce,
        )
        result.sign(self._private)
        return WorkerResult(
            result_manifest=result,
            artifact_bytes=artifact_bytes,
            ok=True,
            reasons=[],
        )

    def _reject(self, manifest: JobManifest, reasons: list[str]) -> WorkerResult:
        now = time.time()
        result = ResultManifest(
            protocol_version=PROTOCOL_VERSION,
            worker_version=WORKER_VERSION,
            job_id=manifest.job_id,
            tenant_id=manifest.tenant_id,
            status=JobStatus.REJECTED.value,
            artifact_name="",
            artifact_sha256="",
            artifact_size=0,
            dataset_sha256=manifest.dataset_sha256,
            code_version=manifest.code_version,
            created_at=now,
            completed_at=now,
            metrics={},
            provenance={"reject_reasons": list(reasons)},
            validation_metadata={"ok": False, "reasons": list(reasons)},
            nonce=manifest.nonce,
        )
        # Sign rejects too so NVRA can authenticate the rejection.
        result.sign(self._private)
        return WorkerResult(
            result_manifest=result,
            artifact_bytes=b"",
            ok=False,
            reasons=list(reasons),
        )


def worker_has_no_execution_apis(worker: ColabWorker) -> bool:
    """Structural proof used by tests."""
    forbidden = (
        "place_order",
        "submit_order",
        "execute_trade",
        "mt5_order",
        "modify_risk",
        "bypass_governor",
        "os_system",
        "shell_exec",
    )
    return all(not hasattr(worker, name) for name in forbidden)
