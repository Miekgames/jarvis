"""
Network diagnostics: local ports, connections, DNS, latency, interfaces.

Scoped deliberately. Port scanning is restricted to this machine -- scanning
someone else's host is the kind of thing that gets a home connection suspended,
and it is not what a personal assistant is for. The restriction is enforced by
resolving the target and refusing anything that is not a loopback address, not
by trusting the string.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import platform
import socket
import subprocess

import time
from typing import Any, Dict, List, Optional

from ..core.proc import run as _run, popen as _popen

logger = logging.getLogger("services.netdiag")

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dependency in practice
    psutil = None  # type: ignore

# Ports worth naming when we find them open.
WELL_KNOWN = {
    22: "SSH", 53: "DNS", 80: "HTTP", 443: "HTTPS", 445: "SMB", 1433: "SQL Server",
    3000: "dev server", 3306: "MySQL", 3389: "RDP", 5000: "dev server",
    5432: "PostgreSQL", 5173: "Vite", 6379: "Redis", 8000: "JARVIS backend",
    8080: "HTTP alt", 8888: "Jupyter", 9000: "dev server", 11434: "Ollama",
    27017: "MongoDB",
}


def _is_local(host: str) -> bool:
    """True only for loopback. Resolution happens first, so 'localhost.evil.com'
    cannot sneak through on the strength of its prefix."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_loopback:
                return False
        except ValueError:
            return False
    return True


class NetDiagService:
    """Read-only network inspection."""

    def __init__(self, settings=None, activity=None):
        self.settings = settings
        self.activity = activity

    # ==================================================================
    # ports
    # ==================================================================
    async def scan_ports(self, host: str = "127.0.0.1", start: int = 1,
                         end: int = 10000, timeout: float = 0.08) -> Dict[str, Any]:
        """Scan a port range on this machine only."""
        host = (host or "127.0.0.1").strip()
        if not _is_local(host):
            raise PermissionError(
                "Port scanning is limited to this machine. '{}' is not a loopback "
                "address.".format(host))

        start = max(1, min(int(start), 65535))
        end = max(start, min(int(end), 65535))
        if end - start > 20000:
            raise ValueError(
                "That range is {} ports. Scan at most 20000 at a time.".format(end - start))

        loop = asyncio.get_running_loop()
        semaphore = asyncio.Semaphore(400)

        async def probe(port: int) -> Optional[int]:
            async with semaphore:
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=timeout)
                    writer.close()
                    return port
                except (asyncio.TimeoutError, OSError):
                    return None

        began = time.perf_counter()
        results = await asyncio.gather(*[probe(p) for p in range(start, end + 1)])
        open_ports = [p for p in results if p]

        listeners = await loop.run_in_executor(None, self._listening_map)
        return {
            "host": host,
            "range": "{}-{}".format(start, end),
            "open": [{"port": p, "service": WELL_KNOWN.get(p, ""),
                      "process": listeners.get(p, "")} for p in open_ports],
            "count": len(open_ports),
            "scanned": end - start + 1,
            "seconds": round(time.perf_counter() - began, 2),
        }

    def _listening_map(self) -> Dict[int, str]:
        """Port to process name, when psutil can see it."""
        if psutil is None:
            return {}
        mapping: Dict[int, str] = {}
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == "LISTEN" and conn.laddr:
                    name = ""
                    if conn.pid:
                        try:
                            name = psutil.Process(conn.pid).name()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            name = "pid {}".format(conn.pid)
                    mapping[conn.laddr.port] = name
        except (psutil.AccessDenied, PermissionError, RuntimeError):
            logger.debug("cannot enumerate connections without elevation")
        return mapping

    async def listening(self) -> Dict[str, Any]:
        """Everything listening on this machine, with the owning process."""
        if psutil is None:
            return {"ok": False, "message": "psutil is not installed."}

        loop = asyncio.get_running_loop()

        def collect() -> List[Dict[str, Any]]:
            found = []
            try:
                for conn in psutil.net_connections(kind="inet"):
                    if conn.status != "LISTEN" or not conn.laddr:
                        continue
                    name = ""
                    if conn.pid:
                        try:
                            name = psutil.Process(conn.pid).name()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            name = ""
                    found.append({
                        "port": conn.laddr.port,
                        "address": conn.laddr.ip,
                        "pid": conn.pid,
                        "process": name,
                        "service": WELL_KNOWN.get(conn.laddr.port, ""),
                    })
            except (psutil.AccessDenied, PermissionError):
                return []
            return found

        listeners = await loop.run_in_executor(None, collect)
        if not listeners:
            return {"ok": False,
                    "message": "Could not read listening sockets. On Windows this "
                               "usually needs an elevated process."}
        listeners.sort(key=lambda item: item["port"])
        return {"ok": True, "listening": listeners, "count": len(listeners)}

    async def port_conflict(self, port: int) -> Dict[str, Any]:
        """Who is holding a port -- the answer to 'address already in use'."""
        port = int(port)
        listeners = await self.listening()
        if not listeners.get("ok"):
            return listeners

        holders = [item for item in listeners["listening"] if item["port"] == port]
        if not holders:
            return {"ok": True, "port": port, "in_use": False,
                    "message": "Nothing is listening on port {}.".format(port)}
        return {"ok": True, "port": port, "in_use": True, "holders": holders,
                "message": "Port {} is held by {}.".format(
                    port, ", ".join(h["process"] or "pid {}".format(h["pid"])
                                    for h in holders))}

    # ==================================================================
    # DNS and latency
    # ==================================================================
    async def dns_lookup(self, hostname: str) -> Dict[str, Any]:
        hostname = (hostname or "").strip()
        if not hostname:
            raise ValueError("Which hostname?")

        loop = asyncio.get_running_loop()
        began = time.perf_counter()
        try:
            infos = await loop.run_in_executor(
                None, lambda: socket.getaddrinfo(hostname, None))
        except socket.gaierror as exc:
            return {"ok": False, "hostname": hostname,
                    "error": "Could not resolve: {}".format(exc.strerror or exc)}

        addresses = []
        for info in infos:
            address = info[4][0]
            family = "IPv6" if info[0] == socket.AF_INET6 else "IPv4"
            if not any(a["address"] == address for a in addresses):
                addresses.append({"address": address, "family": family})

        reverse = ""
        try:
            reverse = await loop.run_in_executor(
                None, lambda: socket.gethostbyaddr(addresses[0]["address"])[0])
        except (socket.herror, socket.gaierror, IndexError, OSError):
            reverse = ""

        return {"ok": True, "hostname": hostname, "addresses": addresses,
                "reverse": reverse,
                "ms": round((time.perf_counter() - began) * 1000, 1)}

    async def latency(self, host: str = "1.1.1.1", port: int = 443,
                      attempts: int = 4) -> Dict[str, Any]:
        """Measure TCP connect time.

        A real measurement rather than ICMP ping, because raw sockets need
        elevation and this needs none -- and it measures what actually matters
        for an application anyway.
        """
        host = (host or "").strip()
        if not host:
            raise ValueError("Which host?")
        attempts = max(1, min(int(attempts), 10))

        timings: List[float] = []
        failures = 0
        for _ in range(attempts):
            began = time.perf_counter()
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, int(port)), timeout=3.0)
                writer.close()
                timings.append((time.perf_counter() - began) * 1000)
            except (asyncio.TimeoutError, OSError):
                failures += 1

        if not timings:
            return {"ok": False, "host": host, "port": port,
                    "error": "No connection succeeded in {} attempt(s).".format(attempts)}

        ordered = sorted(timings)
        return {
            "ok": True, "host": host, "port": port,
            "attempts": attempts, "failed": failures,
            "min_ms": round(ordered[0], 1),
            "max_ms": round(ordered[-1], 1),
            "avg_ms": round(sum(timings) / len(timings), 1),
            "median_ms": round(ordered[len(ordered) // 2], 1),
            "loss": round(failures / attempts, 3),
        }

    # ==================================================================
    # interfaces and connections
    # ==================================================================
    async def interfaces(self) -> Dict[str, Any]:
        if psutil is None:
            return {"ok": False, "message": "psutil is not installed."}

        loop = asyncio.get_running_loop()

        def collect() -> Dict[str, Any]:
            adapters = []
            stats = psutil.net_if_stats()
            counters = psutil.net_io_counters(pernic=True)
            for name, addresses in psutil.net_if_addrs().items():
                entry: Dict[str, Any] = {"name": name, "addresses": [], "up": False}
                for address in addresses:
                    if address.family == socket.AF_INET:
                        entry["addresses"].append({"family": "IPv4", "address": address.address,
                                                   "netmask": address.netmask})
                    elif address.family == socket.AF_INET6:
                        entry["addresses"].append({"family": "IPv6",
                                                   "address": address.address.split("%")[0]})
                if name in stats:
                    entry["up"] = stats[name].isup
                    entry["speed_mbps"] = stats[name].speed or None
                if name in counters:
                    entry["sent"] = counters[name].bytes_sent
                    entry["received"] = counters[name].bytes_recv
                adapters.append(entry)
            return {"adapters": adapters}

        result = await loop.run_in_executor(None, collect)
        active = [a for a in result["adapters"] if a["up"] and a["addresses"]]
        return {"ok": True, "adapters": result["adapters"], "active": len(active),
                "hostname": socket.gethostname()}

    async def connections(self, limit: int = 60) -> Dict[str, Any]:
        """Established outbound connections and which process owns each."""
        if psutil is None:
            return {"ok": False, "message": "psutil is not installed."}

        loop = asyncio.get_running_loop()

        def collect() -> List[Dict[str, Any]]:
            found = []
            try:
                for conn in psutil.net_connections(kind="inet"):
                    if conn.status != "ESTABLISHED" or not conn.raddr:
                        continue
                    name = ""
                    if conn.pid:
                        try:
                            name = psutil.Process(conn.pid).name()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            name = ""
                    found.append({
                        "process": name or ("pid {}".format(conn.pid) if conn.pid else "unknown"),
                        "pid": conn.pid,
                        "remote": "{}:{}".format(conn.raddr.ip, conn.raddr.port),
                        "remote_ip": conn.raddr.ip,
                        "port": conn.raddr.port,
                        "service": WELL_KNOWN.get(conn.raddr.port, ""),
                    })
            except (psutil.AccessDenied, PermissionError):
                return []
            return found

        found = await loop.run_in_executor(None, collect)
        if not found:
            return {"ok": False,
                    "message": "Could not read connections. This usually needs elevation."}

        by_process: Dict[str, int] = {}
        for item in found:
            by_process[item["process"]] = by_process.get(item["process"], 0) + 1

        return {
            "ok": True,
            "connections": found[:limit],
            "total": len(found),
            "by_process": sorted(
                [{"process": p, "connections": n} for p, n in by_process.items()],
                key=lambda item: -item["connections"])[:15],
        }

    async def wifi(self) -> Dict[str, Any]:
        """Wi-Fi details, on Windows, via netsh."""
        if platform.system() != "Windows":
            return {"ok": False,
                    "message": "Wi-Fi details are only implemented for Windows here."}

        loop = asyncio.get_running_loop()

        def run() -> str:
            try:
                completed = _run(
                    ["netsh", "wlan", "show", "interfaces"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return completed.stdout
            except (OSError, subprocess.SubprocessError) as exc:
                return "ERROR: {}".format(exc)

        output = await loop.run_in_executor(None, run)
        if output.startswith("ERROR:") or not output.strip():
            return {"ok": False, "message": "Could not read Wi-Fi state. " + output[:200]}

        fields: Dict[str, str] = {}
        for line in output.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                if key and value.strip():
                    fields[key] = value.strip()

        if "ssid" not in fields:
            return {"ok": False,
                    "message": "No wireless interface is connected."}

        return {
            "ok": True,
            "ssid": fields.get("ssid", ""),
            "signal": fields.get("signal", ""),
            "radio": fields.get("radio type", ""),
            "band": fields.get("band", ""),
            "channel": fields.get("channel", ""),
            "receive_rate": fields.get("receive rate (mbps)", ""),
            "transmit_rate": fields.get("transmit rate (mbps)", ""),
            "authentication": fields.get("authentication", ""),
        }
