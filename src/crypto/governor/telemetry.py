"""Runtime telemetry samples — stdlib / injectable."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

# psutil platform surface varies: Windows builds often lack sensors_temperatures.
_PSUTIL_ERRORS = (ImportError, OSError, RuntimeError, AttributeError, ValueError, TypeError)


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """Point-in-time resource observation. None = UNKNOWN."""

    timestamp_ms: int
    cpu_utilization: float | None  # 0..1
    ram_total_bytes: int | None
    ram_available_bytes: int | None
    process_rss_bytes: int | None
    swap_used_bytes: int | None
    io_wait_ratio: float | None  # 0..1
    disk_latency_ms: float | None
    network_latency_ms: float | None
    network_errors: int | None
    queue_depth: int | None
    queue_age_ms: float | None
    cpu_temp_c: float | None
    on_battery: bool | None


def sample_resources(
    *,
    queue_depth: int | None = None,
    queue_age_ms: float | None = None,
    network_latency_ms: float | None = None,
    network_errors: int | None = None,
    disk_latency_ms: float | None = None,
) -> ResourceSample:
    """Best-effort sample. Missing metrics stay None (UNKNOWN)."""
    now = int(time.time() * 1000)
    cpu = _cpu_util()
    ram_total, ram_avail, swap = _mem()
    rss = _process_rss()
    io_wait = _io_wait()
    return ResourceSample(
        timestamp_ms=now,
        cpu_utilization=cpu,
        ram_total_bytes=ram_total,
        ram_available_bytes=ram_avail,
        process_rss_bytes=rss,
        swap_used_bytes=swap,
        io_wait_ratio=io_wait,
        disk_latency_ms=disk_latency_ms,
        network_latency_ms=network_latency_ms,
        network_errors=network_errors,
        queue_depth=queue_depth,
        queue_age_ms=queue_age_ms,
        cpu_temp_c=_cpu_temp(),
        on_battery=_on_battery(),
    )


def _cpu_util() -> float | None:
    """Rough host CPU util from /proc/stat delta — needs two reads for accuracy.

    Single-shot returns loadavg-based estimate or None.
    """
    if sys.platform == "win32":
        try:
            import psutil

            return max(0.0, min(1.0, float(psutil.cpu_percent(interval=0.0)) / 100.0))
        except _PSUTIL_ERRORS:
            return None
    try:
        load1, _, _ = os.getloadavg()
        n = os.cpu_count() or 1
        return max(0.0, min(1.5, load1 / n))  # may exceed 1 under overload
    except (OSError, AttributeError):
        return None


def _mem() -> tuple[int | None, int | None, int | None]:
    if sys.platform == "win32":
        try:
            import psutil

            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            return int(vm.total), int(vm.available), int(sw.used)
        except _PSUTIL_ERRORS:
            return None, None, None

    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return None, None, None
    try:
        data: dict[str, int] = {}
        for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            k, rest = line.split(":", 1)
            parts = rest.strip().split()
            if parts:
                with suppress(ValueError):
                    data[k] = int(parts[0]) * 1024
        total = data.get("MemTotal")
        avail = data.get("MemAvailable") or data.get("MemFree")
        swap = data.get("SwapTotal", 0) - data.get("SwapFree", 0)
        return total, avail, max(0, swap) if "SwapTotal" in data else None
    except OSError:
        return None, None, None


def _process_rss() -> int | None:
    if sys.platform == "win32":
        try:
            import psutil

            return int(psutil.Process().memory_info().rss)
        except _PSUTIL_ERRORS:
            return None

    status = Path("/proc/self/status")
    if not status.is_file():
        return None
    try:
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return int(parts[1]) * 1024  # kB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _io_wait() -> float | None:
    # Not reliably single-shot without deltas; leave UNKNOWN
    return None


def _cpu_temp() -> float | None:
    if sys.platform == "win32":
        try:
            import psutil

            # sensors_temperatures is often unavailable on Windows builds.
            if not hasattr(psutil, "sensors_temperatures"):
                return None
            temps = psutil.sensors_temperatures()
            if not temps:
                return None
            values = [
                float(item.current)
                for entries in temps.values()
                for item in entries
                if item.current is not None and 0 < float(item.current) < 120
            ]
            return values[0] if values else None
        except _PSUTIL_ERRORS:
            return None

    hwmon = Path("/sys/class/hwmon")
    if not hwmon.is_dir():
        return None
    for d in hwmon.iterdir():
        try:
            for tf in d.glob("temp*_input"):
                raw = int(tf.read_text(encoding="utf-8").strip())
                c = raw / 1000.0
                if 0 < c < 120:
                    return c
        except (OSError, ValueError):
            continue
    return None


def _on_battery() -> bool | None:
    if sys.platform == "win32":
        try:
            import psutil

            if not hasattr(psutil, "sensors_battery"):
                return None
            b = psutil.sensors_battery()
            return None if b is None else not bool(b.power_plugged)
        except _PSUTIL_ERRORS:
            return None

    bat = Path("/sys/class/power_supply")
    if not bat.is_dir():
        return None
    for p in bat.iterdir():
        try:
            t = (p / "type").read_text(encoding="utf-8").strip().lower()
            if t == "mains":
                online = (p / "online").read_text(encoding="utf-8").strip()
                return online != "1"
        except OSError:
            continue
    return None
