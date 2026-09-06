"""Google Colab Free — opportunistic optional provider (lazy, never required).

Security contract:
- HEAVY training/research only
- No secrets, no broker credentials, no execution commands
- Signed job protocol + authenticated worker when available
- Output is untrusted; promotion requires local validation
- Disconnect / missing session => INTERRUPTED or UNKNOWN, never SUCCESS
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .base import ComputeProvider
from .protocol import (
    JobManifest,
    ReplayStore,
    build_job_manifest_from_training_job,
    validate_result_manifest,
)
from .security import assert_no_execution_commands, assert_no_secrets, sanitize_mapping
from .types import (
    JobStatus,
    ProviderCapability,
    ProviderStatus,
    TrainingJob,
    TrainingResult,
)
from .worker import ColabWorker


def _detect_colab_runtime() -> bool:
    """True only when actually running inside a Colab kernel."""
    try:
        import importlib.util

        return importlib.util.find_spec("google.colab") is not None
    except Exception:
        return False


class ColabComputeProvider(ComputeProvider):
    """Opportunistic Colab backend with optional authenticated worker.

    Inject *worker* for in-process CI / local simulation.
    Without worker or Colab runtime → UNKNOWN (never fake SUCCESS).
    """

    name = "colab"

    def __init__(
        self,
        *,
        enabled: bool = False,
        opportunistic: bool = True,
        worker: Optional[ColabWorker] = None,
        private_raw: Optional[bytes] = None,
        public_raw: Optional[bytes] = None,
        artifact_dir: Optional[Path] = None,
        replay: Optional[ReplayStore] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.opportunistic = bool(opportunistic)
        self._worker = worker
        self._private = private_raw
        self._public = public_raw
        self._artifact_dir = Path(artifact_dir) if artifact_dir else None
        self._replay = replay or ReplayStore()
        self._force_status: Optional[ProviderStatus] = None
        self._force_disconnect: bool = False

    def probe(self) -> ProviderCapability:
        if self._force_status is not None:
            return ProviderCapability(
                name=self.name,
                status=self._force_status,
                supports_training=True,
                supports_inference=False,
                notes=("forced",),
            )
        if not self.enabled:
            return ProviderCapability(
                name=self.name,
                status=ProviderStatus.DISABLED,
                supports_training=True,
                supports_inference=False,
                notes=("disabled_by_config",),
            )
        # In-process authenticated worker counts as available for CI/tests.
        if self._worker is not None:
            return ProviderCapability(
                name=self.name,
                status=ProviderStatus.AVAILABLE,
                supports_training=True,
                supports_inference=False,
                notes=("authenticated_worker", "heavy_only"),
            )
        if _detect_colab_runtime():
            return ProviderCapability(
                name=self.name,
                status=ProviderStatus.AVAILABLE,
                supports_training=True,
                supports_inference=False,
                notes=("colab_runtime", "opportunistic", "heavy_only"),
            )
        return ProviderCapability(
            name=self.name,
            status=ProviderStatus.UNAVAILABLE,
            supports_training=True,
            supports_inference=False,
            notes=("no_colab_runtime", "opportunistic"),
        )

    def submit(self, job: TrainingJob, payload: Optional[Mapping[str, Any]] = None) -> TrainingResult:
        safe = sanitize_mapping(payload)
        try:
            assert_no_secrets(safe)
            assert_no_secrets(job.metadata)
            assert_no_execution_commands(safe)
            assert_no_execution_commands(job.metadata)
            assert_no_execution_commands(payload)
        except ValueError as exc:
            job.status = JobStatus.REJECTED
            job.provider = self.name
            job.metadata = {**job.metadata, "reason": str(exc)}
            note = (
                "rejected_execution_command"
                if "execution" in str(exc).lower()
                else "rejected_secret"
            )
            return TrainingResult(job=job, provider_notes=(note,))

        job.provider = self.name

        if not job.is_heavy():
            job.status = JobStatus.REJECTED
            job.metadata = {
                **job.metadata,
                "reason": "colab_rejects_non_heavy_workload",
                "workload_type": job.workload_type,
            }
            return TrainingResult(job=job, provider_notes=("rejected_non_heavy",))

        cap = self.probe()
        if cap.status in (ProviderStatus.DISABLED, ProviderStatus.UNAVAILABLE, ProviderStatus.FAILED):
            job.status = JobStatus.FAILED
            job.metadata = {**job.metadata, "reason": f"provider_{cap.status.value.lower()}"}
            return TrainingResult(job=job, provider_notes=(cap.status.value,))

        if self._force_disconnect or cap.status == ProviderStatus.INTERRUPTED:
            job.status = JobStatus.INTERRUPTED
            job.metadata = {**job.metadata, "reason": "session_disconnected"}
            return TrainingResult(job=job, provider_notes=("interrupted", "not_success"))

        # Authenticated worker path (CI / local simulation / future Colab bridge).
        if self._worker is not None and self._private is not None and self._public is not None:
            return self._submit_via_worker(job, safe)

        # No worker attached — external Colab session required.
        job.status = JobStatus.UNKNOWN
        job.metadata = {
            **job.metadata,
            "reason": "colab_requires_external_session",
            "payload_keys": sorted(safe.keys()),
            "tenant_id": job.tenant_id,
            "workload_type": job.workload_type,
        }
        if job.tenant_id:
            job.provenance = {
                **job.provenance,
                "tenant_id": job.tenant_id,
                "provider": self.name,
            }
        return TrainingResult(job=job, provider_notes=("external_session_required", "untrusted_output"))

    def _submit_via_worker(self, job: TrainingJob, safe_payload: dict[str, Any]) -> TrainingResult:
        assert self._worker is not None and self._private is not None and self._public is not None
        params = dict(safe_payload)
        params.update(sanitize_mapping(job.metadata))
        # Strip non-training metadata keys that are not params.
        for k in list(params.keys()):
            if k in ("reason", "artifact_path", "artifact_hash", "tenant_id"):
                params.pop(k, None)

        manifest = build_job_manifest_from_training_job(
            job,
            private_raw=self._private,
            training_params=params,
        )
        wr = self._worker.process(manifest)

        if not wr.ok or wr.result_manifest.status != JobStatus.SUCCESS.value:
            job.status = JobStatus.REJECTED if wr.result_manifest.status == JobStatus.REJECTED.value else JobStatus.FAILED
            job.metadata = {
                **job.metadata,
                "reason": ",".join(wr.reasons) or wr.result_manifest.status,
                "result_manifest": wr.result_manifest.to_dict(),
            }
            return TrainingResult(
                job=job,
                provider_notes=tuple(wr.reasons) or ("worker_rejected",),
            )

        ok, reasons = validate_result_manifest(
            wr.result_manifest,
            public_raw=self._public,
            expected_job=manifest,
            expected_tenant=job.tenant_id,
        )
        if not ok:
            job.status = JobStatus.FAILED
            job.metadata = {
                **job.metadata,
                "reason": "result_validation_failed:" + ",".join(reasons),
            }
            return TrainingResult(job=job, provider_notes=tuple(reasons))

        # Verify artifact checksum matches manifest.
        from .protocol import sha256_bytes

        actual = sha256_bytes(wr.artifact_bytes)
        if actual != wr.result_manifest.artifact_sha256:
            job.status = JobStatus.FAILED
            job.metadata = {**job.metadata, "reason": "artifact_hash_mismatch"}
            return TrainingResult(job=job, provider_notes=("artifact_hash_mismatch",))

        artifact_path = ""
        if self._artifact_dir is not None:
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            out = self._artifact_dir / wr.result_manifest.artifact_name
            out.write_bytes(wr.artifact_bytes)
            artifact_path = str(out)
            job.artifact_ref = artifact_path
        else:
            job.artifact_ref = f"colab://{job.job_id}/{actual[:16]}"

        job.status = JobStatus.SUCCESS
        job.metrics = dict(wr.result_manifest.metrics)
        job.provenance = {
            **job.provenance,
            **wr.result_manifest.provenance,
            "result_signature_ok": True,
            "protocol_version": wr.result_manifest.protocol_version,
            "worker_version": wr.result_manifest.worker_version,
        }
        meta = {
            **job.metadata,
            "artifact_hash": actual,
            "result_manifest": wr.result_manifest.to_dict(),
            "tenant_id": job.tenant_id,
        }
        if artifact_path:
            meta["artifact_path"] = artifact_path
        job.metadata = meta
        return TrainingResult(
            job=job,
            artifact_hash=actual,
            checkpoint_hash=actual,
            provider_notes=("colab_worker_completed", "signed_result"),
        )
