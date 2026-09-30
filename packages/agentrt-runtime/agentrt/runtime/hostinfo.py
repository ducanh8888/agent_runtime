"""Best-effort POSIX host and daemon process information."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


PROC_ROOT = Path("/proc")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _proc_stat(pid: int) -> tuple[int, int] | None:
    try:
        raw = (PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _processes() -> dict[int, tuple[int, int]]:
    processes = {}
    try:
        entries = PROC_ROOT.iterdir()
    except OSError:
        return processes
    for entry in entries:
        if entry.name.isdigit():
            stat = _proc_stat(int(entry.name))
            if stat is not None:
                processes[int(entry.name)] = stat
    return processes


def _rss(pid: int) -> int:
    try:
        pages = int((PROC_ROOT / str(pid) / "statm").read_text().split()[1])
        return pages * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return 0


def _cmdline(pid: int, limit: int = 200) -> str:
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
        return (
            raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()[:limit]
        )
    except OSError:
        return ""


def _tree(processes: dict[int, tuple[int, int]], root: int) -> list[int]:
    descendants = [root]
    for pid in descendants:
        descendants.extend(
            child
            for child, (ppid, _) in processes.items()
            if ppid == pid and child not in descendants
        )
    return descendants


def collect(daemon_pid: int) -> tuple[dict | None, str | None]:
    """Return memory/process tree measurements and daemon start time."""
    if os.name != "posix":
        return None, None
    try:
        values = {}
        for line in (PROC_ROOT / "meminfo").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value.split()[0]) * 1024
        processes = _processes()
        daemon_tree = _tree(processes, daemon_pid) if daemon_pid in processes else []
        children = [pid for pid, (ppid, _) in processes.items() if ppid == daemon_pid]
        top = []
        for child in children:
            members = _tree(processes, child)
            top.append(
                {
                    "pid": child,
                    "rss_bytes": sum(_rss(pid) for pid in members),
                    "cmdline": _cmdline(child),
                }
            )
        top.sort(key=lambda item: item["rss_bytes"], reverse=True)
        start = None
        if daemon_pid in processes:
            ticks = processes[daemon_pid][1]
            boot = next(
                (
                    int(line.split()[1])
                    for line in (PROC_ROOT / "stat").read_text().splitlines()
                    if line.startswith("btime ")
                ),
                None,
            )
            if boot is not None:
                start = datetime.fromtimestamp(
                    boot + ticks / os.sysconf("SC_CLK_TCK")
                ).isoformat()
        return {
            "total_memory_bytes": values.get("MemTotal"),
            "available_memory_bytes": values.get("MemAvailable"),
            "daemon_process_tree_rss_bytes": sum(_rss(pid) for pid in daemon_tree),
            "biggest_children": top[:10],
        }, start
    except (OSError, ValueError, IndexError):
        return None, None
