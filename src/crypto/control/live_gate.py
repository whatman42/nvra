"""Live gate controller — auto enable/disable with linear self-recovery.

Policy (locked):
* Human one-time authorization remains in env:
    NVRA_REAL_TRADING_ENABLE + NVRA_REAL_TRADING_CONFIRM=I_UNDERSTAND_REAL_TRADING
* This module NEVER writes those env vars or invents the confirmation phrase.
* When env authorization is present AND preflight/checklist passes → enable trading.
* On soft failure → disable trading, linear backoff retry (1m, 2m, 3m, …), probe only.
* On hard failure → stop recovery loop, require human intervention, notify.
* No orders are placed from this module.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SOFT_REASON_PREFIXES = (
    "connect_failed",
    "clock_or_health_not_ok",
    "markets_not_loaded",
    "permission_probe_failed",
    "network",
    "timeout",
    "rate_limit",
    "exchange_unavailable",
    "degraded",
    "5xx",
    "temporary",
)

HARD_REASON_PREFIXES = (
    "NVRA_REAL_TRADING_ENABLE_not_set",
    "real_trading_confirmation_missing",
    "real_mode_disabled_by_policy",
    "withdrawal_permission_granted",
    "not_authenticated",
    "trading_permission_not_granted",
    "demo_or_sandbox_blocks_production_orders",
    "auth_failed",
    "invalid_api",
    "emergency_stop",
)


class LiveGateState(Enum):
    IDLE = auto()
    LIVE_ENABLED = auto()
    RECOVERING = auto()
    HARD_STOP = auto()


class FailureClass(Enum):
    NONE = auto()
    SOFT = auto()
    HARD = auto()


@dataclass(frozen=True, slots=True)
class LiveGateSnapshot:
    state: str
    live_enabled: bool
    failure_class: str
    attempt: int
    next_retry_in_seconds: float | None
    last_reasons: tuple[str, ...] = ()
    message: str = ""


@dataclass
class LiveGateConfig:
    """Linear backoff: attempt 1 → 1 min, 2 → 2 min, … capped at max_backoff_minutes."""

    base_backoff_minutes: int = 1
    max_backoff_minutes: int = 60
    continue_at_cap: bool = True
    hard_stop_remind_minutes: int = 30


class SupportsTradingGate(Protocol):
    @property
    def trading_enabled(self) -> bool: ...

    def enable_trading(self, enabled: bool = True) -> None: ...


NotifyFn = Callable[[str, str], None]


def classify_reasons(reasons: list[str] | tuple[str, ...]) -> FailureClass:
    if not reasons:
        return FailureClass.NONE
    lowered = [r.lower() for r in reasons]
    for hard in HARD_REASON_PREFIXES:
        h = hard.lower()
        if any(h in r for r in lowered):
            return FailureClass.HARD
    for soft in SOFT_REASON_PREFIXES:
        s = soft.lower()
        if any(s in r for r in lowered):
            return FailureClass.SOFT
    return FailureClass.SOFT


def linear_backoff_seconds(attempt: int, *, base_minutes: int = 1, max_minutes: int = 60) -> float:
    n = max(1, int(attempt))
    minutes = min(max_minutes, base_minutes * n)
    return float(minutes * 60)


class LiveGateController:
    """Checklist-driven live enable with linear self-recovery.

    preflight_fn() must return an object with:
      - live_submit_allowed: bool
      - reasons: sequence[str]
    Compatible with TokocryptoPreflightReport.
    """

    def __init__(
        self,
        adapter: SupportsTradingGate,
        *,
        preflight_fn: Callable[[], Any],
        notify: NotifyFn | None = None,
        config: LiveGateConfig | None = None,
        mono_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._preflight_fn = preflight_fn
        self._notify = notify or (
            lambda level, msg: logger.log(
                logging.ERROR if level in {"error", "critical"} else logging.INFO, msg
            )
        )
        self._cfg = config or LiveGateConfig()
        self._mono = mono_fn or time.monotonic
        self._sleep = sleep_fn
        self._state = LiveGateState.IDLE
        self._attempt = 0
        self._next_retry_mono: float | None = None
        self._last_reasons: tuple[str, ...] = ()
        self._last_hard_remind_mono: float | None = None
        self._message = ""

    @property
    def state(self) -> LiveGateState:
        return self._state

    def snapshot(self) -> LiveGateSnapshot:
        now = self._mono()
        nxt = None
        if self._next_retry_mono is not None:
            nxt = max(0.0, self._next_retry_mono - now)
        return LiveGateSnapshot(
            state=self._state.name,
            live_enabled=bool(self._adapter.trading_enabled),
            failure_class=(
                FailureClass.HARD.name
                if self._state is LiveGateState.HARD_STOP
                else FailureClass.SOFT.name
                if self._state is LiveGateState.RECOVERING
                else FailureClass.NONE.name
            ),
            attempt=self._attempt,
            next_retry_in_seconds=nxt,
            last_reasons=self._last_reasons,
            message=self._message,
        )

    def evaluate(self) -> LiveGateSnapshot:
        if self._state is LiveGateState.HARD_STOP:
            self._maybe_hard_remind()
            return self.snapshot()

        report = self._run_preflight()
        allowed = bool(getattr(report, "live_submit_allowed", False))
        reasons = tuple(getattr(report, "reasons", ()) or ())
        self._last_reasons = reasons

        if allowed:
            return self._on_success()

        klass = classify_reasons(list(reasons))
        if klass is FailureClass.HARD:
            return self._on_hard(reasons)
        return self._on_soft(reasons)

    def tick(self) -> LiveGateSnapshot:
        if self._state is LiveGateState.HARD_STOP:
            self._maybe_hard_remind()
            return self.snapshot()
        if self._state is LiveGateState.RECOVERING:
            now = self._mono()
            if self._next_retry_mono is not None and now < self._next_retry_mono:
                return self.snapshot()
            return self.evaluate()
        return self.evaluate()

    def force_hard_stop(self, reason: str) -> LiveGateSnapshot:
        self._disable_quiet()
        self._state = LiveGateState.HARD_STOP
        self._message = reason
        self._last_reasons = (reason,)
        self._notify("critical", f"NVRA live HARD_STOP: {reason}")
        return self.snapshot()

    def reset_hard_stop(self) -> LiveGateSnapshot:
        if self._state is not LiveGateState.HARD_STOP:
            return self.snapshot()
        self._state = LiveGateState.IDLE
        self._attempt = 0
        self._next_retry_mono = None
        self._message = "hard_stop_reset_by_operator"
        self._notify("info", "NVRA live hard-stop reset by operator; re-evaluating")
        return self.evaluate()

    def _run_preflight(self) -> Any:
        try:
            return self._preflight_fn()
        except Exception as exc:  # noqa: BLE001
            return _SyntheticReport(
                live_submit_allowed=False,
                reasons=(f"connect_failed:{type(exc).__name__}",),
            )

    def _on_success(self) -> LiveGateSnapshot:
        was_recovering = self._state is LiveGateState.RECOVERING
        try:
            if not self._adapter.trading_enabled:
                self._adapter.enable_trading(True)
        except PermissionError as exc:
            return self._on_hard((str(exc), "real_trading_confirmation_missing"))
        except Exception as exc:  # noqa: BLE001
            return self._on_soft((f"connect_failed:{type(exc).__name__}",))

        self._state = LiveGateState.LIVE_ENABLED
        self._attempt = 0
        self._next_retry_mono = None
        self._message = "live_enabled"
        if was_recovering:
            self._notify("info", "NVRA live recovered — trading re-enabled")
        else:
            self._notify("info", "NVRA live enabled — checklist passed")
        return self.snapshot()

    def _on_soft(self, reasons: tuple[str, ...]) -> LiveGateSnapshot:
        self._disable_quiet()
        self._attempt = max(1, self._attempt + 1)
        delay = linear_backoff_seconds(
            self._attempt,
            base_minutes=self._cfg.base_backoff_minutes,
            max_minutes=self._cfg.max_backoff_minutes,
        )
        if (
            not self._cfg.continue_at_cap
            and self._attempt * self._cfg.base_backoff_minutes > self._cfg.max_backoff_minutes
        ):
            return self._on_hard(reasons + ("recovery_cap_exceeded",))

        self._state = LiveGateState.RECOVERING
        self._next_retry_mono = self._mono() + delay
        self._message = f"soft_fail attempt={self._attempt} retry_in={int(delay)}s"
        self._notify(
            "warn",
            f"NVRA live disabled (soft): {', '.join(reasons) or 'unknown'}; "
            f"retry in {int(delay // 60)}m (attempt {self._attempt})",
        )
        if self._sleep is not None:
            self._sleep(delay)
        return self.snapshot()

    def _on_hard(self, reasons: tuple[str, ...]) -> LiveGateSnapshot:
        self._disable_quiet()
        self._state = LiveGateState.HARD_STOP
        self._next_retry_mono = None
        self._message = "hard_stop"
        self._last_hard_remind_mono = self._mono()
        self._notify(
            "critical",
            f"NVRA live HARD_STOP — human required: {', '.join(reasons) or 'unknown'}",
        )
        return self.snapshot()

    def _disable_quiet(self) -> None:
        try:
            if self._adapter.trading_enabled:
                self._adapter.enable_trading(False)
        except Exception:  # noqa: BLE001
            pass

    def _maybe_hard_remind(self) -> None:
        mins = self._cfg.hard_stop_remind_minutes
        if mins <= 0:
            return
        now = self._mono()
        last = self._last_hard_remind_mono or 0.0
        if now - last >= mins * 60:
            self._last_hard_remind_mono = now
            self._notify(
                "critical",
                f"NVRA live still HARD_STOP: {', '.join(self._last_reasons) or self._message}",
            )


@dataclass
class _SyntheticReport:
    live_submit_allowed: bool
    reasons: tuple[str, ...]
