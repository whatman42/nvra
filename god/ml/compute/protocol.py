"""Signed remote compute job protocol (versioned, fail-closed).

LOCAL remains the only trusted execution path. Colab is untrusted heavy compute.
Manifests never carry secrets or execution commands.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from .security import assert_no_execution_commands, assert_no_secrets, sanitize_mapping
from .types import JobStatus, TrainingJob, WorkloadType

PROTOCOL_VERSION = "1.0"
WORKER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = frozenset({"1.0"})
DEFAULT_TTL_SEC = 3600
MAX_CLOCK_SKEW_SEC = 300


def _canonical_json(obj: Mapping[str, Any]) -> bytes:
    """Deterministic JSON for signing (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_raw_32, public_raw_32). Never hard-code keys."""
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_raw, pub_raw


def _load_private(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def _load_public(raw: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(raw)


def sign_bytes(private_raw: bytes, payload: bytes) -> str:
    sig = _load_private(private_raw).sign(payload)
    return base64.urlsafe_b64encode(sig).decode("ascii")


def verify_bytes(public_raw: bytes, payload: bytes, signature_b64: str) -> bool:
    try:
        sig = base64.urlsafe_b64decode(signature_b64.encode("ascii"))
        _load_public(public_raw).verify(sig, payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


@dataclass
class JobManifest:
    """Canonical signed job request — no secrets, no execution commands."""

    protocol_version: str = PROTOCOL_VERSION
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tenant_id: str = ""
    workload_type: str = WorkloadType.HEAVY.value
    model_type: str = "random_forest"
    model_id: str = ""
    model_version: str = "1"
    dataset_id: str = ""
    dataset_sha256: str = ""
    code_version: str = ""
    training_config_hash: str = ""
    requested_resources: dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = DEFAULT_TTL_SEC
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    provenance: dict[str, Any] = field(default_factory=dict)
    # Allowlisted training params only (no code, no shell)
    training_params: dict[str, Any] = field(default_factory=dict)
    signature: str = ""

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = float(self.created_at) + int(self.timeout_sec or DEFAULT_TTL_SEC)

    def body_dict(self) -> dict[str, Any]:
        """Fields that are signed (excludes signature itself)."""
        d = asdict(self)
        d.pop("signature", None)
        return d

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobManifest":
        return cls(
            protocol_version=str(data.get("protocol_version") or PROTOCOL_VERSION),
            job_id=str(data.get("job_id") or uuid.uuid4().hex),
            tenant_id=str(data.get("tenant_id") or ""),
            workload_type=str(data.get("workload_type") or WorkloadType.HEAVY.value),
            model_type=str(data.get("model_type") or "random_forest"),
            model_id=str(data.get("model_id") or ""),
            model_version=str(data.get("model_version") or "1"),
            dataset_id=str(data.get("dataset_id") or ""),
            dataset_sha256=str(data.get("dataset_sha256") or ""),
            code_version=str(data.get("code_version") or ""),
            training_config_hash=str(data.get("training_config_hash") or ""),
            requested_resources=dict(data.get("requested_resources") or {}),
            timeout_sec=int(data.get("timeout_sec") or DEFAULT_TTL_SEC),
            created_at=float(data.get("created_at") or time.time()),
            expires_at=float(data.get("expires_at") or 0.0),
            nonce=str(data.get("nonce") or secrets.token_hex(16)),
            provenance=dict(data.get("provenance") or {}),
            training_params=dict(data.get("training_params") or {}),
            signature=str(data.get("signature") or ""),
        )

    def sign(self, private_raw: bytes) -> "JobManifest":
        payload = _canonical_json(self.body_dict())
        self.signature = sign_bytes(private_raw, payload)
        return self

    def verify(self, public_raw: bytes) -> bool:
        if not self.signature:
            return False
        return verify_bytes(public_raw, _canonical_json(self.body_dict()), self.signature)


@dataclass
class ResultManifest:
    """Signed result from worker — untrusted until verified by NVRA."""

    protocol_version: str = PROTOCOL_VERSION
    worker_version: str = WORKER_VERSION
    job_id: str = ""
    tenant_id: str = ""
    status: str = JobStatus.FAILED.value
    artifact_name: str = ""
    artifact_sha256: str = ""
    artifact_size: int = 0
    dataset_sha256: str = ""
    code_version: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)
    metrics: dict[str, float] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    validation_metadata: dict[str, Any] = field(default_factory=dict)
    nonce: str = ""
    signature: str = ""

    def body_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("signature", None)
        return d

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResultManifest":
        return cls(
            protocol_version=str(data.get("protocol_version") or PROTOCOL_VERSION),
            worker_version=str(data.get("worker_version") or ""),
            job_id=str(data.get("job_id") or ""),
            tenant_id=str(data.get("tenant_id") or ""),
            status=str(data.get("status") or JobStatus.FAILED.value),
            artifact_name=str(data.get("artifact_name") or ""),
            artifact_sha256=str(data.get("artifact_sha256") or ""),
            artifact_size=int(data.get("artifact_size") or 0),
            dataset_sha256=str(data.get("dataset_sha256") or ""),
            code_version=str(data.get("code_version") or ""),
            created_at=float(data.get("created_at") or time.time()),
            completed_at=float(data.get("completed_at") or time.time()),
            metrics=dict(data.get("metrics") or {}),
            provenance=dict(data.get("provenance") or {}),
            validation_metadata=dict(data.get("validation_metadata") or {}),
            nonce=str(data.get("nonce") or ""),
            signature=str(data.get("signature") or ""),
        )

    def sign(self, private_raw: bytes) -> "ResultManifest":
        self.signature = sign_bytes(private_raw, _canonical_json(self.body_dict()))
        return self

    def verify(self, public_raw: bytes) -> bool:
        if not self.signature:
            return False
        return verify_bytes(public_raw, _canonical_json(self.body_dict()), self.signature)


class ReplayStore:
    """In-memory replay protection. Production may swap for durable store."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}  # key -> expires_at

    def _key(self, job_id: str, nonce: str) -> str:
        return f"{job_id}:{nonce}"

    def seen(self, job_id: str, nonce: str) -> bool:
        self._purge()
        return self._key(job_id, nonce) in self._seen

    def record(self, job_id: str, nonce: str, expires_at: float) -> None:
        self._seen[self._key(job_id, nonce)] = float(expires_at)

    def _purge(self) -> None:
        now = time.time()
        dead = [k for k, exp in self._seen.items() if exp < now - MAX_CLOCK_SKEW_SEC]
        for k in dead:
            self._seen.pop(k, None)


def build_job_manifest_from_training_job(
    job: TrainingJob,
    *,
    private_raw: bytes,
    training_params: Optional[Mapping[str, Any]] = None,
    ttl_sec: Optional[int] = None,
) -> JobManifest:
    """Build + sign a JobManifest from TrainingJob. Sanitizes metadata."""
    # Assert on RAW input first — sanitize_mapping strips secret-like keys
    # (e.g. mt5_order matches fragment "mt5") and would hide execution commands.
    # Execution checks run before secret checks so order/shell keys get a precise reject.
    assert_no_execution_commands(job.metadata)
    assert_no_secrets(job.metadata)
    safe_meta = sanitize_mapping(job.metadata)
    assert_no_execution_commands(safe_meta)
    assert_no_secrets(safe_meta)

    raw_params = training_params or {}
    assert_no_execution_commands(raw_params)
    assert_no_secrets(raw_params)
    params = sanitize_mapping(raw_params)
    assert_no_execution_commands(params)
    assert_no_secrets(params)

    ttl = int(ttl_sec if ttl_sec is not None else job.timeout_sec or DEFAULT_TTL_SEC)
    now = time.time()
    m = JobManifest(
        protocol_version=PROTOCOL_VERSION,
        job_id=job.job_id,
        tenant_id=job.tenant_id,
        workload_type=job.workload_type,
        model_type=job.model_type or "random_forest",
        model_id=job.model_id,
        model_version=job.model_version,
        dataset_id=job.dataset_id,
        dataset_sha256=job.dataset_hash,
        code_version=job.code_version,
        training_config_hash=job.training_config_hash,
        requested_resources=dict(job.requested_resources or {}),
        timeout_sec=ttl,
        created_at=now,
        expires_at=now + ttl,
        nonce=secrets.token_hex(16),
        provenance=dict(job.provenance or {}),
        training_params=params,
    )
    return m.sign(private_raw)


def validate_job_manifest(
    manifest: JobManifest,
    *,
    public_raw: bytes,
    authorized_tenant: str = "",
    replay: Optional[ReplayStore] = None,
    now: Optional[float] = None,
) -> tuple[bool, list[str]]:
    """Fail-closed validation of a signed job."""
    reasons: list[str] = []
    t = float(now if now is not None else time.time())

    if manifest.protocol_version not in SUPPORTED_PROTOCOLS:
        reasons.append(f"protocol_mismatch:{manifest.protocol_version}")

    if not manifest.verify(public_raw):
        reasons.append("invalid_signature")

    if t > float(manifest.expires_at) + MAX_CLOCK_SKEW_SEC:
        reasons.append("job_expired")

    if t + MAX_CLOCK_SKEW_SEC < float(manifest.created_at) - 1:
        reasons.append("created_in_future")

    if not manifest.nonce or len(manifest.nonce) < 16:
        reasons.append("missing_or_short_nonce")

    if authorized_tenant and manifest.tenant_id != authorized_tenant:
        reasons.append("tenant_mismatch")

    wt = str(manifest.workload_type).lower()
    if wt not in {WorkloadType.HEAVY.value, "heavy", "training_heavy", "neural", "ensemble_heavy", "hparam_search", "backtest_heavy"}:
        reasons.append(f"workload_not_heavy:{manifest.workload_type}")

    try:
        assert_no_execution_commands(manifest.training_params)
        assert_no_secrets(manifest.training_params)
        assert_no_execution_commands(manifest.provenance)
        assert_no_secrets(manifest.provenance)
    except ValueError as exc:
        reasons.append(f"payload_rejected:{exc}")

    if replay is not None:
        if replay.seen(manifest.job_id, manifest.nonce):
            reasons.append("replay_detected")

    return (not reasons), reasons


def validate_result_manifest(
    result: ResultManifest,
    *,
    public_raw: bytes,
    expected_job: Optional[JobManifest] = None,
    expected_tenant: str = "",
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    if result.protocol_version not in SUPPORTED_PROTOCOLS:
        reasons.append(f"protocol_mismatch:{result.protocol_version}")

    if not result.worker_version:
        reasons.append("missing_worker_version")

    if not result.verify(public_raw):
        reasons.append("invalid_result_signature")

    if expected_job is not None:
        if result.job_id != expected_job.job_id:
            reasons.append("job_id_mismatch")
        if result.tenant_id != expected_job.tenant_id:
            reasons.append("result_tenant_mismatch")
        if expected_job.dataset_sha256 and result.dataset_sha256 != expected_job.dataset_sha256:
            reasons.append("dataset_sha256_mismatch")
        if expected_job.nonce and result.nonce and result.nonce != expected_job.nonce:
            reasons.append("nonce_mismatch")

    if expected_tenant and result.tenant_id != expected_tenant:
        reasons.append("tenant_mismatch")

    if result.status not in (JobStatus.SUCCESS.value, "SUCCESS", "COMPLETED"):
        reasons.append(f"status_not_success:{result.status}")

    if not result.artifact_sha256 or len(result.artifact_sha256) < 16:
        reasons.append("missing_or_short_artifact_hash")

    if result.artifact_size < 0:
        reasons.append("negative_artifact_size")

    return (not reasons), reasons


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
