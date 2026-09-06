"""Optional multi-provider compute backends (Local / Colab / Kaggle).

Cloud providers are opportunistic accelerators for HEAVY training/research only.
They never participate in signal, risk, execution, or SAFE_MODE paths.
LOCAL = trusted execution + inference + risk
COLAB/KAGGLE = untrusted heavy compute only
"""
from .base import ComputeProvider
from .colab import ColabComputeProvider
from .config import ColabConfig, ComputeConfig, KaggleConfig, LocalConfig, load_compute_config
from .kaggle import KaggleComputeProvider
from .local import LocalComputeProvider
from .protocol import (
    PROTOCOL_VERSION,
    WORKER_VERSION,
    JobManifest,
    ReplayStore,
    ResultManifest,
    build_job_manifest_from_training_job,
    generate_keypair,
    validate_job_manifest,
    validate_result_manifest,
)
from .security import (
    assert_no_execution_commands,
    assert_no_secrets,
    sanitize_and_guard,
    sanitize_mapping,
)
from .selector import select_provider
from .types import (
    JobStatus,
    ProviderCapability,
    ProviderStatus,
    TrainingJob,
    TrainingResult,
    WorkloadType,
)
from .validation import ArtifactValidationResult, validate_training_result
from .worker import ColabWorker, worker_has_no_execution_apis

__all__ = [
    "ComputeProvider",
    "LocalComputeProvider",
    "ColabComputeProvider",
    "KaggleComputeProvider",
    "select_provider",
    "ComputeConfig",
    "LocalConfig",
    "ColabConfig",
    "KaggleConfig",
    "load_compute_config",
    "TrainingJob",
    "TrainingResult",
    "JobStatus",
    "ProviderStatus",
    "ProviderCapability",
    "WorkloadType",
    "sanitize_mapping",
    "sanitize_and_guard",
    "assert_no_secrets",
    "assert_no_execution_commands",
    "validate_training_result",
    "ArtifactValidationResult",
    "PROTOCOL_VERSION",
    "WORKER_VERSION",
    "JobManifest",
    "ResultManifest",
    "ReplayStore",
    "generate_keypair",
    "build_job_manifest_from_training_job",
    "validate_job_manifest",
    "validate_result_manifest",
    "ColabWorker",
    "worker_has_no_execution_apis",
]
