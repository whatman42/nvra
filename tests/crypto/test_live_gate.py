"""Tests for LiveGateController — no real orders, mock adapter/preflight."""

from __future__ import annotations

from crypto.control.live_gate import (
    FailureClass,
    LiveGateConfig,
    LiveGateController,
    LiveGateState,
    classify_reasons,
    linear_backoff_seconds,
)


class FakeAdapter:
    def __init__(self, *, sandbox: bool = False) -> None:
        self._sandbox = sandbox
        self._trading_enabled = False
        self._fail_enable = False

    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    def enable_trading(self, enabled: bool = True) -> None:
        if enabled and self._fail_enable:
            raise PermissionError("REAL trading requires NVRA_REAL_TRADING_ENABLE")
        self._trading_enabled = bool(enabled)


class FakeReport:
    def __init__(self, allowed: bool, reasons: tuple[str, ...] = ()) -> None:
        self.live_submit_allowed = allowed
        self.reasons = reasons


def test_linear_backoff_minutes() -> None:
    assert linear_backoff_seconds(1) == 60
    assert linear_backoff_seconds(2) == 120
    assert linear_backoff_seconds(3) == 180
    assert linear_backoff_seconds(100, max_minutes=60) == 3600


def test_classify_hard_vs_soft() -> None:
    assert classify_reasons(["real_trading_confirmation_missing"]) is FailureClass.HARD
    assert classify_reasons(["withdrawal_permission_granted_prefer_trade_only_key"]) is FailureClass.HARD
    assert classify_reasons(["connect_failed:Timeout"]) is FailureClass.SOFT
    assert classify_reasons(["clock_or_health_not_ok"]) is FailureClass.SOFT
    assert classify_reasons([]) is FailureClass.NONE


def test_enable_when_checklist_ok() -> None:
    ad = FakeAdapter()
    notes: list[tuple[str, str]] = []
    ctl = LiveGateController(
        ad,
        preflight_fn=lambda: FakeReport(True),
        notify=lambda lvl, msg: notes.append((lvl, msg)),
        mono_fn=lambda: 1000.0,
    )
    snap = ctl.evaluate()
    assert snap.state == LiveGateState.LIVE_ENABLED.name
    assert ad.trading_enabled is True
    assert any("enabled" in m.lower() for _, m in notes)


def test_soft_fail_then_recover() -> None:
    ad = FakeAdapter()
    clock = {"t": 0.0}
    results = iter(
        [
            FakeReport(False, ("connect_failed:NetworkError",)),
            FakeReport(False, ("clock_or_health_not_ok",)),
            FakeReport(True),
        ]
    )
    notes: list[str] = []
    ctl = LiveGateController(
        ad,
        preflight_fn=lambda: next(results),
        notify=lambda lvl, msg: notes.append(msg),
        mono_fn=lambda: clock["t"],
        config=LiveGateConfig(base_backoff_minutes=1, max_backoff_minutes=60),
    )
    s1 = ctl.evaluate()
    assert s1.state == LiveGateState.RECOVERING.name
    assert ad.trading_enabled is False
    assert s1.attempt == 1
    assert s1.next_retry_in_seconds == 60

    clock["t"] = 30
    s_wait = ctl.tick()
    assert s_wait.state == LiveGateState.RECOVERING.name

    clock["t"] = 60
    s2 = ctl.tick()
    assert s2.state == LiveGateState.RECOVERING.name
    assert s2.attempt == 2
    assert s2.next_retry_in_seconds == 120

    clock["t"] = 60 + 120
    s3 = ctl.tick()
    assert s3.state == LiveGateState.LIVE_ENABLED.name
    assert ad.trading_enabled is True
    assert any("recovered" in n.lower() for n in notes)


def test_hard_stop_no_auto_retry() -> None:
    ad = FakeAdapter()
    ctl = LiveGateController(
        ad,
        preflight_fn=lambda: FakeReport(False, ("real_trading_confirmation_missing",)),
        notify=lambda *_: None,
        mono_fn=lambda: 0.0,
    )
    s = ctl.evaluate()
    assert s.state == LiveGateState.HARD_STOP.name
    assert ad.trading_enabled is False
    s2 = ctl.tick()
    assert s2.state == LiveGateState.HARD_STOP.name


def test_hard_stop_reset_by_operator() -> None:
    ad = FakeAdapter()
    phase = {"n": 0}

    def pf():
        phase["n"] += 1
        if phase["n"] == 1:
            return FakeReport(False, ("real_trading_confirmation_missing",))
        return FakeReport(True)

    ctl = LiveGateController(
        ad,
        preflight_fn=pf,
        notify=lambda *_: None,
        mono_fn=lambda: 0.0,
    )
    ctl.evaluate()
    assert ctl.state is LiveGateState.HARD_STOP
    snap = ctl.reset_hard_stop()
    assert snap.state == LiveGateState.LIVE_ENABLED.name
    assert ad.trading_enabled is True


def test_enable_permission_error_is_hard() -> None:
    ad = FakeAdapter()
    ad._fail_enable = True
    ctl = LiveGateController(
        ad,
        preflight_fn=lambda: FakeReport(True),
        notify=lambda *_: None,
        mono_fn=lambda: 0.0,
    )
    s = ctl.evaluate()
    assert s.state == LiveGateState.HARD_STOP.name
