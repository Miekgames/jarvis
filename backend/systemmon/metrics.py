"""
Real-time system monitoring.

Samples on a fixed interval, keeps a short in-memory ring for the live UI and
persists to SQLite for the historical graphs. Sustained-threshold alerts are
debounced so a one-second CPU spike doesn't generate a notification, but
fifteen minutes of hot GPU does.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import subprocess

import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from ..core.proc import run as _run, popen as _popen
from ..core.events import Events, bus
from ..core.util import now_iso, since, utcnow

logger = logging.getLogger("jarvis.metrics")

try:
    import psutil
except ImportError:  # pragma: no cover - dependency guard
    psutil = None  # type: ignore

RING_SIZE = 240  # ~12 minutes at a 3s interval


class MetricsService:
    def __init__(self, db, settings, settings_store, activity, notifications=None):
        self.db = db
        self.settings = settings
        self.settings_store = settings_store
        self.activity = activity
        self.notifications = notifications

        self.ring: Deque[Dict[str, Any]] = deque(maxlen=RING_SIZE)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_net: Optional[Any] = None
        self._last_net_time: float = 0.0
        self._breach_since: Dict[str, float] = {}
        self._alerted: Dict[str, float] = {}
        self._gpu_available: Optional[bool] = None
        self._boot_time = psutil.boot_time() if psutil else time.time()

    # ==================================================================
    async def start(self) -> None:
        if psutil is None:
            logger.warning("psutil unavailable -- system monitoring disabled")
            return
        if self._running:
            return
        self._running = True
        # Prime the per-process CPU counters so the first sample isn't all zeros,
        # then take one immediately so the dashboard has data on first paint.
        psutil.cpu_percent(interval=None)
        await asyncio.sleep(0.15)
        try:
            self.ring.append(await asyncio.get_event_loop().run_in_executor(None, self.sample))
        except Exception:
            logger.exception("initial metrics sample failed")
        self._task = asyncio.create_task(self._loop(), name="metrics-sampler")
        logger.info("metrics sampler started (%ss interval)", self.settings.METRICS_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    # ==================================================================
    async def _loop(self) -> None:
        persist_every = max(1, int(15 / max(1, self.settings.METRICS_INTERVAL_SECONDS)))
        counter = 0
        while self._running:
            try:
                monitoring = await self.settings_store.get("monitoring", {})
                if monitoring.get("enabled", True):
                    sample = await asyncio.get_event_loop().run_in_executor(None, self.sample)
                    self.ring.append(sample)
                    bus.publish(Events.METRICS, sample, source="system")

                    counter += 1
                    if counter % persist_every == 0:
                        await self._persist(sample)
                        await self._prune()
                    await self._check_thresholds(sample, monitoring)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("metrics sample failed")
            await asyncio.sleep(self.settings.METRICS_INTERVAL_SECONDS)

    # ==================================================================
    def sample(self) -> Dict[str, Any]:
        """Blocking -- always called via the executor."""
        if psutil is None:
            return {"ts": now_iso(), "cpu": 0, "mem": 0, "disk": 0}

        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage("C:\\" if platform.system() == "Windows" else "/")
        except OSError:
            disk = None

        net_up = net_down = 0.0
        try:
            counters = psutil.net_io_counters()
            now = time.monotonic()
            if self._last_net is not None and now > self._last_net_time:
                elapsed = now - self._last_net_time
                net_up = max(0.0, (counters.bytes_sent - self._last_net.bytes_sent) / elapsed)
                net_down = max(0.0, (counters.bytes_recv - self._last_net.bytes_recv) / elapsed)
            self._last_net, self._last_net_time = counters, now
        except Exception:
            pass

        battery_percent = None
        power_plugged = None
        try:
            battery = psutil.sensors_battery()
            if battery:
                battery_percent = round(battery.percent, 1)
                power_plugged = battery.power_plugged
        except Exception:
            pass

        gpu = self._gpu()
        temperatures = self._temperatures()

        return {
            "ts": now_iso(),
            "cpu": round(cpu, 1),
            "cpu_per_core": [round(c, 1) for c in psutil.cpu_percent(percpu=True)],
            "cpu_freq_mhz": _safe(lambda: round(psutil.cpu_freq().current)),
            "mem": round(memory.percent, 1),
            "mem_used_gb": round(memory.used / 1_073_741_824, 2),
            "mem_total_gb": round(memory.total / 1_073_741_824, 2),
            "disk": round(disk.percent, 1) if disk else 0.0,
            "disk_free_gb": round(disk.free / 1_073_741_824, 1) if disk else 0.0,
            "disk_total_gb": round(disk.total / 1_073_741_824, 1) if disk else 0.0,
            "net_up": round(net_up, 1),
            "net_down": round(net_down, 1),
            "gpu": gpu.get("load"),
            "gpu_mem": gpu.get("memory"),
            "gpu_temp": gpu.get("temperature"),
            "gpu_name": gpu.get("name"),
            "battery": battery_percent,
            "power_plugged": power_plugged,
            "temps": temperatures,
            "uptime_seconds": int(time.time() - self._boot_time),
            "processes": len(psutil.pids()),
        }

    # ------------------------------------------------------------------
    def _gpu(self) -> Dict[str, Any]:
        """NVIDIA via nvidia-smi. Probed once; skipped forever if absent."""
        if self._gpu_available is False:
            return {}
        if self._gpu_available is None:
            self._gpu_available = shutil.which("nvidia-smi") is not None
            if not self._gpu_available:
                logger.info("nvidia-smi not found -- GPU metrics unavailable")
                return {}
        try:
            result = _run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return {}
            name, load, used, total, temperature = [
                p.strip() for p in result.stdout.strip().splitlines()[0].split(",")
            ]
            return {
                "name": name,
                "load": float(load),
                "memory": round(float(used) / float(total) * 100, 1) if float(total) else None,
                "memory_used_mb": float(used),
                "temperature": float(temperature),
            }
        except Exception:
            self._gpu_available = False
            return {}

    @staticmethod
    def _temperatures() -> Dict[str, float]:
        if psutil is None or not hasattr(psutil, "sensors_temperatures"):
            return {}
        try:
            readings = psutil.sensors_temperatures() or {}
        except Exception:
            return {}
        out: Dict[str, float] = {}
        for chip, entries in readings.items():
            for entry in entries:
                if entry.current:
                    label = entry.label or chip
                    out[label] = round(entry.current, 1)
        return out

    # ==================================================================
    async def _persist(self, sample: Dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO metrics (ts, cpu, mem, disk, net_up, net_down, gpu, gpu_mem,"
            " battery, temps) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sample["ts"], sample["cpu"], sample["mem"], sample["disk"],
                sample["net_up"], sample["net_down"], sample.get("gpu"), sample.get("gpu_mem"),
                sample.get("battery"), json.dumps(sample.get("temps") or {}),
            ),
        )

    async def _prune(self) -> None:
        cutoff = since(self.settings.METRICS_RETENTION_HOURS * 3600)
        await self.db.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))

    # ==================================================================
    async def _check_thresholds(self, sample: Dict[str, Any], monitoring: Dict[str, Any]) -> None:
        sustained = float(monitoring.get("sustained_seconds", 300))
        checks = [
            ("cpu", sample.get("cpu"), monitoring.get("cpu_alert", 92), "CPU usage"),
            ("mem", sample.get("mem"), monitoring.get("mem_alert", 92), "Memory usage"),
            ("disk", sample.get("disk"), monitoring.get("disk_alert", 94), "Disk usage"),
            ("gpu_temp", sample.get("gpu_temp"), monitoring.get("gpu_temp_alert", 84), "GPU temperature"),
        ]
        now = time.monotonic()

        for key, value, threshold, label in checks:
            if value is None or threshold in (None, 0):
                continue
            if float(value) >= float(threshold):
                self._breach_since.setdefault(key, now)
                elapsed = now - self._breach_since[key]
                # Re-alert at most once an hour per metric.
                if elapsed >= sustained and now - self._alerted.get(key, 0) > 3600:
                    self._alerted[key] = now
                    minutes = int(elapsed // 60) or 1
                    unit = "°C" if "temp" in key else "%"
                    message = (
                        f"{label} has been at or above {threshold}{unit} for the last "
                        f"{minutes} minute{'s' if minutes != 1 else ''} (currently {value}{unit})."
                    )
                    bus.publish(Events.ALERT, {"metric": key, "value": value, "threshold": threshold,
                                               "message": message}, source="system")
                    await self.activity.record(message, level="warn", source="system")
                    if self.notifications:
                        await self.notifications.create(
                            title=f"{label} elevated", body=message, priority="high", source="system"
                        )
            else:
                self._breach_since.pop(key, None)

    # ==================================================================
    def latest(self) -> Dict[str, Any]:
        return self.ring[-1] if self.ring else {}

    def recent(self, count: int = 120) -> List[Dict[str, Any]]:
        return list(self.ring)[-count:]

    async def history(self, hours: int = 6, max_points: int = 400) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM metrics WHERE ts > ? ORDER BY ts ASC", (since(hours * 3600),)
        )
        if len(rows) > max_points:
            stride = len(rows) // max_points + 1
            rows = rows[::stride]
        for row in rows:
            try:
                row["temps"] = json.loads(row.get("temps") or "{}")
            except Exception:
                row["temps"] = {}
        return rows

    # ==================================================================
    def static_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "os": f"{platform.system()} {platform.release()}",
            "os_version": platform.version(),
            "machine": platform.machine(),
            "hostname": platform.node(),
            "python": platform.python_version(),
        }
        if psutil is None:
            return info
        info.update(
            {
                "cpu_cores_physical": psutil.cpu_count(logical=False),
                "cpu_cores_logical": psutil.cpu_count(logical=True),
                "memory_total_gb": round(psutil.virtual_memory().total / 1_073_741_824, 1),
                "boot_time": datetime.fromtimestamp(self._boot_time).isoformat(),
                "uptime_seconds": int(time.time() - self._boot_time),
            }
        )
        try:
            info["processor"] = platform.processor() or platform.machine()
        except Exception:
            pass
        try:
            info["disks"] = [
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": round(psutil.disk_usage(part.mountpoint).total / 1_073_741_824, 1),
                    "percent": psutil.disk_usage(part.mountpoint).percent,
                }
                for part in psutil.disk_partitions(all=False)
                if part.fstype
            ]
        except Exception:
            info["disks"] = []
        gpu = self._gpu()
        if gpu:
            info["gpu"] = gpu.get("name")
        return info

    async def network_reachable(self, timeout: float = 3.0) -> bool:
        """Used by the health service and offline mode."""
        import socket

        def _probe() -> bool:
            for host, port in (("1.1.1.1", 53), ("8.8.8.8", 53)):
                try:
                    with socket.create_connection((host, port), timeout=timeout):
                        return True
                except OSError:
                    continue
            return False

        return await asyncio.get_event_loop().run_in_executor(None, _probe)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default
