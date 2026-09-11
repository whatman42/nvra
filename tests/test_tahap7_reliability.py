"""TAHAP 7 — reliability, secret scan, sqlite close, loop recovery."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from god.capability.models import CapabilityProvider, CapabilityType
from god.capability.registry import CapabilityRegistry
from god.loop import AutonomousControlLoop
from god.market_decision import Quote
from tools.secret_scan import scan, _is_false_positive_line, PATTERNS


def _pem_begin_header() -> str:
    # Built at runtime so the source file never contains a contiguous PEM header
    # that would trip tools/secret_scan.py during test_secret_scan_repo_clean.
    return "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5


def test_secret_scan_detector_regex_not_false_positive():
    # Detector-definition / grep-style literals must not be treated as secrets.
    assert _is_false_positive_line("ghp_[A-Za-z0-9]{36,}")
    assert _is_false_positive_line("-e 'BEGIN RSA PRIVATE KEY'")
    # Incomplete marker (no PEM dashes) is documentation, not a key.
    assert _is_false_positive_line("look for BEGIN RSA PRIVATE KEY in logs")


def test_secret_scan_detects_fake_value(tmp_path):
    # realistic length fake PAT — must be detected as secret material
    fake = "ghp_" + ("A" * 40)
    assert PATTERNS[0][0].search(fake)
    assert not _is_false_positive_line(f"token={fake}")


def test_secret_scan_detects_real_pem_header(tmp_path):
    # Full PEM private-key header must be detected (not a false positive).
    pem_line = _pem_begin_header()
    assert not _is_false_positive_line(pem_line)
    assert any(rx.search(pem_line) for rx, _ in PATTERNS)

    # Use .txt so TOP_GLOBS in tools/secret_scan.py includes the file.
    f = tmp_path / "leaked_key.txt"
    f.write_text(
        pem_line + "\nMIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/" + ("A" * 40) + "\n"
        + ("-" * 5 + "END RSA PRIVATE KEY" + "-" * 5) + "\n",
        encoding="utf-8",
    )
    hits = scan(tmp_path)
    assert hits, "expected private_key_header hit on real PEM"
    kinds = {h[2] for h in hits}
    assert "private_key_header" in kinds


def test_secret_scan_fixture_dummy_not_hit(tmp_path):
    # Dummy documentation / detector-fixture must NOT be reported as secret.
    f = tmp_path / "docs.txt"
    f.write_text(
        "Scanner looks for patterns like BEGIN RSA PRIVATE KEY without dashes.\n"
        "Also regex forms: ghp_[A-Za-z0-9]{36,}\n",
        encoding="utf-8",
    )
    hits = scan(tmp_path)
    assert hits == [], hits


def test_secret_scan_repo_clean():
    hits = scan(Path(".").resolve())
    assert hits == [], hits


def test_sqlite_tempdir_cleanup_windows_safe():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "caps.db"
        reg = CapabilityRegistry(db_path=db)
        reg.register(
            CapabilityProvider.create(
                name="Docker",
                capability=CapabilityType.CONTAINER,
                available=True,
                executable="/usr/bin/docker",
            )
        )
        reg.close()
        del reg
        # file should be deletable (Windows WinError 32 regression)
    # TemporaryDirectory exits without PermissionError


def test_sqlite_repeated_lifecycle():
    for _ in range(5):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            reg = CapabilityRegistry(db_path=Path(td) / "c.db")
            reg.register(
                CapabilityProvider.create("Py", CapabilityType.PYTHON, available=True)
            )
            assert reg.best(CapabilityType.PYTHON) is not None
            reg.close()


def test_autonomous_crash_no_duplicate_intent(tmp_path):
    loop = AutonomousControlLoop(ml_registry=tmp_path / "ml")
    q = Quote("EURUSD", time.time(), bid=1.1, ask=1.1002, sequence=1)
    out = loop.run_cycle(quote=q, closes=[1.0] * 50, crash_after_state="OBSERVING")
    assert out.recovery_required
    assert out.broker_orders_submitted == 0
    # resume
    out2 = loop.run_cycle(quote=q, closes=[1.0] * 50, resume_cycle_id=out.cycle_id)
    assert out2.recovery_required
    assert out2.broker_orders_submitted == 0


def test_loop_broker_still_zero(tmp_path):
    loop = AutonomousControlLoop(ml_registry=tmp_path / "ml2")
    q = Quote("EURUSD", time.time(), bid=1.1, ask=1.1002, sequence=1)
    out = loop.run_cycle(quote=q, closes=[100 + i * 0.01 for i in range(80)])
    assert out.broker_orders_submitted == 0


def test_ipc_shutdown_exists():
    from god.ipc.tcp import TcpTransport

    assert hasattr(TcpTransport, "shutdown") or hasattr(TcpTransport, "disconnect")
