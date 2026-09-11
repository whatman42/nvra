"""Signed compute protocol, Colab worker, replay, tenant, artifact round-trip."""
from __future__ import annotations

from pathlib import Path

import pytest

from god.ml.compute import (
    PROTOCOL_VERSION,
    WORKER_VERSION,
    ColabComputeProvider,
    ColabWorker,
    JobManifest,
    JobStatus,
    LocalComputeProvider,
    ReplayStore,
    TrainingJob,
    WorkloadType,
    build_job_manifest_from_training_job,
    generate_keypair,
    select_provider,
    validate_job_manifest,
    validate_result_manifest,
    validate_training_result,
    worker_has_no_execution_apis,
)
from god.ml.compute.protocol import sign_bytes, verify_bytes
from god.ml.registry import ModelRegistry, ModelRecord


@pytest.fixture
def keys():
    return generate_keypair()


@pytest.fixture
def worker(keys):
    priv, pub = keys
    return ColabWorker(public_raw=pub, private_raw=priv, authorized_tenant="tenant-A")


def test_keypair_sign_verify(keys):
    priv, pub = keys
    msg = b"canonical-payload"
    sig = sign_bytes(priv, msg)
    assert verify_bytes(pub, msg, sig)
    assert not verify_bytes(pub, b"tampered", sig)


def test_job_manifest_sign_and_verify(keys):
    priv, pub = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="abc123" * 8,
        model_type="random_forest",
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv, training_params={"n_estimators": 5})
    assert m.signature
    assert m.verify(pub)
    ok, reasons = validate_job_manifest(m, public_raw=pub, authorized_tenant="tenant-A")
    assert ok, reasons


def test_job_manifest_tamper_detected(keys):
    priv, pub = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="deadbeef" * 4,
        model_type="random_forest",
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv)
    m.dataset_sha256 = "tampered" * 4
    assert not m.verify(pub)
    ok, reasons = validate_job_manifest(m, public_raw=pub, authorized_tenant="tenant-A")
    assert not ok
    assert "invalid_signature" in reasons


def test_job_expired_rejected(keys):
    priv, pub = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="deadbeef" * 4,
        model_type="random_forest",
        timeout_sec=1,
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv, ttl_sec=1)
    m.expires_at = m.created_at - 10_000
    m.sign(priv)
    ok, reasons = validate_job_manifest(m, public_raw=pub, authorized_tenant="tenant-A")
    assert not ok
    assert "job_expired" in reasons


def test_replay_protection(keys):
    priv, pub = keys
    store = ReplayStore()
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="deadbeef" * 4,
        model_type="random_forest",
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv)
    ok, _ = validate_job_manifest(m, public_raw=pub, authorized_tenant="tenant-A", replay=store)
    assert ok
    store.record(m.job_id, m.nonce, m.expires_at)
    ok2, reasons = validate_job_manifest(m, public_raw=pub, authorized_tenant="tenant-A", replay=store)
    assert not ok2
    assert "replay_detected" in reasons


def test_cross_tenant_job_rejected(keys):
    priv, pub = keys
    job = TrainingJob(
        tenant_id="tenant-B",
        model_id="m1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="deadbeef" * 4,
        model_type="random_forest",
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv)
    ok, reasons = validate_job_manifest(m, public_raw=pub, authorized_tenant="tenant-A")
    assert not ok
    assert "tenant_mismatch" in reasons


def test_light_workload_rejected_by_protocol(keys):
    priv, pub = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m1",
        workload_type=WorkloadType.LIGHT.value,
        dataset_hash="deadbeef" * 4,
        model_type="random_forest",
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv)
    ok, reasons = validate_job_manifest(m, public_raw=pub, authorized_tenant="tenant-A")
    assert not ok
    assert any("workload_not_heavy" in r for r in reasons)


def test_worker_happy_path_artifact_roundtrip(keys, worker, tmp_path):
    priv, pub = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="rf1",
        model_version="1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="a" * 64,
        model_type="random_forest",
        dataset_id="ds1",
    )
    m = build_job_manifest_from_training_job(
        job, private_raw=priv, training_params={"n_estimators": 5, "n_samples": 32}
    )
    wr = worker.process(m)
    assert wr.ok
    assert wr.result_manifest.status == JobStatus.SUCCESS.value
    assert wr.artifact_bytes
    assert wr.result_manifest.artifact_sha256
    assert wr.result_manifest.verify(pub)
    ok, reasons = validate_result_manifest(
        wr.result_manifest, public_raw=pub, expected_job=m, expected_tenant="tenant-A"
    )
    assert ok, reasons


def test_worker_rejects_exec_injection(keys, worker):
    priv, _ = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="b" * 64,
        model_type="random_forest",
    )
    m = build_job_manifest_from_training_job(
        job, private_raw=priv, training_params={"code": "import os; os.system('id')"}
    )
    wr = worker.process(m)
    assert not wr.ok
    assert wr.result_manifest.status == JobStatus.REJECTED.value


def test_worker_rejects_shell_and_order_params(keys, worker):
    """Reject shell/order params at build-time; worker also rejects if present.

    mt5_order must not be silently stripped by secret-sanitize then accepted.
    """
    priv, _ = keys
    for bad in (
        {"shell": "rm -rf /"},
        {"place_order": {"symbol": "EURUSD"}},
        {"subprocess": ["ls"]},
        {"mt5_order": 1},
    ):
        job = TrainingJob(
            tenant_id="tenant-A",
            model_id="m",
            workload_type=WorkloadType.HEAVY.value,
            dataset_hash="c" * 64,
            model_type="random_forest",
        )
        with pytest.raises(ValueError, match="execution command rejected|forbidden"):
            build_job_manifest_from_training_job(job, private_raw=priv, training_params=bad)


def test_build_rejects_execution_commands(keys):
    """Explicit unit: build rejects place_order, mt5_order, shell, subprocess."""
    from god.ml.compute.security import assert_no_execution_commands

    priv, _ = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="c" * 64,
        model_type="random_forest",
    )
    for bad, needle in (
        ({"place_order": {"symbol": "EURUSD"}}, "place_order"),
        ({"mt5_order": 1}, "mt5_order"),
        ({"shell": "id"}, "shell"),
        ({"subprocess": ["ls"]}, "subprocess"),
    ):
        with pytest.raises(ValueError, match="execution command rejected"):
            build_job_manifest_from_training_job(job, private_raw=priv, training_params=bad)
        with pytest.raises(ValueError, match="execution command rejected"):
            assert_no_execution_commands(bad)


def test_worker_rejects_mt5_order_when_present_on_manifest(keys, worker):
    """Defensive: if a JobManifest is constructed with mt5_order, worker must reject."""
    from god.ml.compute.protocol import JobManifest
    from god.ml.compute.types import WorkloadType as WT

    priv, pub = keys
    # Bypass build helper — simulate adversarial/tampered training_params already on manifest
    m = JobManifest(
        protocol_version="1.0",
        job_id="adversarial-mt5",
        tenant_id="tenant-A",
        workload_type=WorkloadType.HEAVY.value,
        model_type="random_forest",
        model_id="m",
        dataset_sha256="c" * 64,
        training_params={"mt5_order": 1, "n_estimators": 3},
    )
    m.sign(priv)
    wr = worker.process(m)
    assert not wr.ok
    assert wr.reasons, "worker must report rejection reasons for mt5_order"
    joined = " ".join(wr.reasons).lower()
    assert "mt5_order" in joined or "execution" in joined or "forbidden" in joined


def test_sanitize_does_not_hide_execution_commands():
    """sanitize_and_guard must reject mt5_order, not strip-and-accept."""
    from god.ml.compute.security import sanitize_and_guard

    with pytest.raises(ValueError, match="execution command rejected"):
        sanitize_and_guard({"mt5_order": 1})
    with pytest.raises(ValueError, match="execution command rejected"):
        sanitize_and_guard({"place_order": True})
    with pytest.raises(ValueError, match="execution command rejected"):
        sanitize_and_guard({"shell": "rm"})


def test_worker_rejects_disallowed_model_type(keys, worker):
    priv, _ = keys
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="d" * 64,
        model_type="arbitrary_neural_net_custom",
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv)
    wr = worker.process(m)
    assert not wr.ok
    assert any("model_type_not_allowed" in r for r in wr.reasons)


def test_worker_replay_second_submit(keys):
    priv, pub = keys
    store = ReplayStore()
    w = ColabWorker(public_raw=pub, private_raw=priv, authorized_tenant="tenant-A", replay=store)
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="e" * 64,
        model_type="random_forest",
    )
    m = build_job_manifest_from_training_job(job, private_raw=priv, training_params={"n_estimators": 3})
    wr1 = w.process(m)
    assert wr1.ok
    wr2 = w.process(m)
    assert not wr2.ok
    assert any("replay" in r for r in wr2.reasons)


def test_worker_has_no_execution_apis(worker):
    assert worker_has_no_execution_apis(worker)


def test_colab_provider_with_worker_success(keys, tmp_path):
    priv, pub = keys
    w = ColabWorker(public_raw=pub, private_raw=priv, authorized_tenant="tenant-A")
    colab = ColabComputeProvider(
        enabled=True,
        worker=w,
        private_raw=priv,
        public_raw=pub,
        artifact_dir=tmp_path,
    )
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m1",
        model_version="1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="f" * 64,
        model_type="random_forest",
    )
    result = colab.submit(job, {"n_estimators": 5, "n_samples": 32})
    assert result.job.status == JobStatus.SUCCESS
    assert result.artifact_hash
    assert Path(result.job.metadata["artifact_path"]).is_file()
    v = validate_training_result(
        result,
        expected_dataset_hash="f" * 64,
        artifact_path=result.job.metadata["artifact_path"],
    )
    assert v.ok and v.eligible_for_promotion


def test_colab_provider_cross_tenant_rejected(keys, tmp_path):
    priv, pub = keys
    w = ColabWorker(public_raw=pub, private_raw=priv, authorized_tenant="tenant-A")
    colab = ColabComputeProvider(
        enabled=True, worker=w, private_raw=priv, public_raw=pub, artifact_dir=tmp_path
    )
    job = TrainingJob(
        tenant_id="tenant-EVIL",
        model_id="m1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="g" * 64,
        model_type="random_forest",
    )
    result = colab.submit(job, {"n_estimators": 3})
    assert result.job.status in (JobStatus.REJECTED, JobStatus.FAILED)
    assert not validate_training_result(
        result, expected_dataset_hash="g" * 64, require_resolvable_artifact=False
    ).eligible_for_promotion


def test_promotion_from_signed_colab_result(keys, tmp_path):
    priv, pub = keys
    w = ColabWorker(public_raw=pub, private_raw=priv, authorized_tenant="tenant-A")
    colab = ColabComputeProvider(
        enabled=True, worker=w, private_raw=priv, public_raw=pub, artifact_dir=tmp_path / "arts"
    )
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="promo",
        model_version="1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="h" * 64,
        model_type="random_forest",
    )
    result = colab.submit(job, {"n_estimators": 4})
    assert result.job.status == JobStatus.SUCCESS

    reg = ModelRegistry(tmp_path / "reg")
    reg._records.append(
        ModelRecord(
            model_id="promo",
            model_version="1",
            status="candidate",
            features_version="f1",
            dataset_hash="h" * 64,
        )
    )
    reg._save()
    promoted = reg.promote_from_compute(result, expected_dataset_hash="h" * 64)
    assert promoted.status == "champion"


def test_invalid_signature_cannot_promote(keys, tmp_path):
    priv, pub = keys
    w = ColabWorker(public_raw=pub, private_raw=priv, authorized_tenant="tenant-A")
    colab = ColabComputeProvider(
        enabled=True, worker=w, private_raw=priv, public_raw=pub, artifact_dir=tmp_path
    )
    job = TrainingJob(
        tenant_id="tenant-A",
        model_id="m",
        model_version="1",
        workload_type=WorkloadType.HEAVY.value,
        dataset_hash="i" * 64,
        model_type="random_forest",
    )
    result = colab.submit(job, {"n_estimators": 3})
    # Tamper artifact after the fact
    path = Path(result.job.metadata["artifact_path"])
    path.write_bytes(path.read_bytes() + b"X")
    v = validate_training_result(
        result, expected_dataset_hash="i" * 64, artifact_path=path
    )
    assert not v.ok
    assert "artifact_hash_mismatch" in v.reasons


def test_colab_unavailable_falls_back_local():
    from god.ml.compute import ComputeConfig

    cfg = ComputeConfig(provider="colab")
    colab = ColabComputeProvider(enabled=True)  # no worker, not in Colab
    p = select_provider(cfg, colab=colab, job=TrainingJob(workload_type=WorkloadType.HEAVY.value))
    assert isinstance(p, LocalComputeProvider)


def test_protocol_versions_exported():
    assert PROTOCOL_VERSION == "1.0"
    assert WORKER_VERSION == "1.0.0"
