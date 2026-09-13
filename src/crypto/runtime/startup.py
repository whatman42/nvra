"""Explicit NVRA startup composition root.

This module composes existing startup/recovery components without owning any
trading, risk, or broker business logic.  Individual stages are injectable so
hosts and tests can supply real broker/reconciliation implementations without
creating new import coupling.
"""
from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable

from crypto.recovery.safe_mode import SafeModeController
from crypto.runtime.migrate import MigrationResult, open_and_migrate
from crypto.runtime.paths import PathResolver

logger = logging.getLogger(__name__)

_STARTUP_STATE: "StartupState"
_STARTUP_STATE_LOCK = threading.RLock()


def get_startup_state() -> "StartupState":
    """Return the latest composition-root state for read-only observers."""
    with _STARTUP_STATE_LOCK:
        return _STARTUP_STATE


def _publish_startup_state(state: "StartupState") -> None:
    global _STARTUP_STATE
    with _STARTUP_STATE_LOCK:
        _STARTUP_STATE = state


class StartupState(Enum):
    INIT = auto()
    LICENSE_CHECK = auto()
    LOAD_STATE = auto()
    BROKER_CONNECT = auto()
    RECONCILIATION = auto()
    RISK_GOVERNOR = auto()
    READY = auto()
    RUNNING = auto()
    SAFE_MODE = auto()
    FAILED = auto()


_STARTUP_STATE = StartupState.INIT


@dataclass
class StartupContext:
    resolver: PathResolver
    argv: list[str]
    state: StartupState = StartupState.INIT
    safe_mode: SafeModeController = field(default_factory=SafeModeController)
    migration: MigrationResult | None = None
    hardware: object | None = None
    policy: object | None = None
    governor: object | None = None
    exchange: object | None = None
    reconciliation: object | None = None
    license_status: str = "NOT_CHECKED"
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StartupResult:
    ok: bool
    state: StartupState
    context: StartupContext


Stage = Callable[[StartupContext], bool]


def _retry_stage(name: str, stage: Stage, context: StartupContext, *, attempts: int = 5) -> bool:
    """Run a startup stage with bounded SAFE_MODE recovery retries."""
    fast = os.environ.get("NVRA_STARTUP_FAST", "").lower() in {"1", "true", "yes"}
    if fast:
        attempts = min(attempts, 2)
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            logger.info("startup stage=%s attempt=%d/%d", name, attempt, attempts)
            if stage(context):
                if context.safe_mode.active:
                    context.safe_mode.try_exit(
                        components_healthy=True,
                        exchange_ok=True,
                        reconciliation_ok=True,
                        execution_consistent=True,
                        market_data_fresh=True,
                        no_unresolved_critical=True,
                        mono=time.monotonic(),
                    )
                return True
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("startup stage=%s failed", name)
            context.errors.append(f"{name}: {last_error}")

        if attempt < attempts:
            context.state = StartupState.SAFE_MODE
            _publish_startup_state(context.state)
            context.safe_mode.enter(
                f"startup stage failed: {name}", mono=time.monotonic()
            )
            logger.warning("startup SAFE_MODE stage=%s retrying", name)
            if not fast:
                time.sleep(min(2.0, 0.25 * attempt))

    context.state = StartupState.SAFE_MODE
    _publish_startup_state(context.state)
    context.safe_mode.enter(
        f"startup stage exhausted: {name} {last_error}", mono=time.monotonic()
    )
    return False


def _license_device(ctx: StartupContext) -> bool:
    """Validate device/license when configured; development remains local-only."""
    from god.licensing.guard import check_device

    account_id = os.environ.get("NVRA_LICENSE_ACCOUNT_ID", "unconfigured")
    service_url = os.environ.get("NVRA_LICENSE_SERVICE_URL", "")
    identity_path = ctx.resolver.state_dir / "device_identity.json"
    result = check_device(account_id, identity_path, service_url)
    ctx.license_status = result.status
    if not result.allowed:
        raise RuntimeError(f"device/license denied: {result.status}")
    logger.info("startup license/device check: %s", result.status)
    return True


def _load_state(ctx: StartupContext) -> bool:
    """Run the existing crash-safe SQLite migration path."""
    from contextlib import suppress

    conn, result = open_and_migrate(ctx.resolver.sqlite_path(), ctx.resolver.backups_dir)
    ctx.migration = result
    if conn is not None:
        # Checkpoint WAL before close — avoids Windows temp-dir lock issues.
        with suppress(Exception):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with suppress(Exception):
            conn.close()
    if not result.ok:
        raise RuntimeError(result.detail)
    logger.info("startup state loaded: %s", result.detail)
    return True


def _connect_data_broker(ctx: StartupContext) -> bool:
    """Connect an explicitly configured exchange; otherwise keep PAPER offline-safe."""
    exchange_id = os.environ.get("NVRA_EXCHANGE_ID", "").strip()
    if not exchange_id:
        logger.info("startup broker stage: no exchange configured; PAPER-safe")
        return True

    from crypto.core.credentials import create_credential_store
    from crypto.exchanges.factory import create_exchange_adapter

    store = create_credential_store(allow_in_memory=False)
    sandbox = os.environ.get("NVRA_EXCHANGE_SANDBOX", "1").lower() in {"1", "true", "yes"}
    account_id = os.environ.get("NVRA_EXCHANGE_ACCOUNT_ID", "default")
    adapter = create_exchange_adapter(exchange_id, store, account_id, sandbox=sandbox)
    adapter.connect()
    adapter.health_check()
    ctx.exchange = adapter
    logger.info("startup broker connected: %s", exchange_id)
    return True


def _reconcile(ctx: StartupContext) -> bool:
    """Perform reconciliation when a host supplies a local snapshot.

    The production codebase intentionally has no single persisted portfolio
    snapshot provider.  We therefore do not fabricate one here; configured
    adapters are health-checked above and callers may inject a reconciliation
    stage for their portfolio source.
    """
    if ctx.exchange is None:
        logger.info("startup reconciliation: deferred (no broker configured)")
        return True
    logger.info("startup reconciliation: broker connected; portfolio source deferred")
    return True


def _risk_governor(ctx: StartupContext) -> bool:
    """Initialize the existing risk authority and computational governor."""
    from crypto.governor import GovernorThresholds, ResourceGovernor
    from crypto.hardware import build_snapshot, save_snapshot
    from crypto.risk.engine import RiskEngine
    from crypto.risk.policy import RiskPolicy

    ctx.hardware = build_snapshot(storage_path=ctx.resolver.root)
    ctx.policy = RiskPolicy()
    RiskEngine(ctx.policy)
    ctx.governor = ResourceGovernor(ctx.hardware.budget, GovernorThresholds())
    ctx.governor.evaluate()
    save_snapshot(ctx.resolver.data_dir / "hardware_snapshot.json", ctx.hardware)
    logger.info("startup risk/governor initialized")
    return True


def run_startup(
    resolver: PathResolver,
    argv: list[str] | None = None,
    *,
    stages: dict[str, Stage] | None = None,
) -> StartupResult:
    """Execute the explicit startup composition root in deterministic order."""
    ctx = StartupContext(resolver=resolver, argv=list(argv or []))
    default_stages: dict[str, Stage] = {
        "license_device": _license_device,
        "load_state": _load_state,
        "data_broker": _connect_data_broker,
        "reconciliation": _reconcile,
        "risk_governor": _risk_governor,
    }
    if stages:
        default_stages.update(stages)

    sequence = (
        (StartupState.LICENSE_CHECK, "license_device"),
        (StartupState.LOAD_STATE, "load_state"),
        (StartupState.BROKER_CONNECT, "data_broker"),
        (StartupState.RECONCILIATION, "reconciliation"),
        (StartupState.RISK_GOVERNOR, "risk_governor"),
    )

    for state, name in sequence:
        ctx.state = state
        _publish_startup_state(state)
        logger.info("startup transition → %s", state.name)
        if not _retry_stage(name, default_stages[name], ctx):
            return StartupResult(False, StartupState.SAFE_MODE, ctx)

    ctx.state = StartupState.READY
    _publish_startup_state(ctx.state)
    logger.info("startup transition → READY")
    ctx.state = StartupState.RUNNING
    _publish_startup_state(ctx.state)
    logger.info("startup transition → RUNNING")
    return StartupResult(True, StartupState.RUNNING, ctx)
