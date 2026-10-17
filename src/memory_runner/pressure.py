"""Memory readings and admission policy, kept independent of process execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


@dataclass(frozen=True)
class Reading:
    level: int | None
    swap_mb: float | None = None
    compressor_mb: float | None = None
    sampled_at: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> Reading:
        if not isinstance(value, dict):
            raise ValueError("memory reading must be a JSON object")
        level = value.get("level")
        if level is not None and (type(level) is not int or level not in (1, 2, 4)):
            raise ValueError("memory level must be 1, 2, 4 or null")
        numbers = {}
        for field in ("swap_mb", "compressor_mb", "sampled_at"):
            number = value.get(field, 0.0 if field == "sampled_at" else None)
            if field == "sampled_at" and number is None:
                raise ValueError("sampled_at must be a nonnegative finite number")
            if number is not None:
                if isinstance(number, bool) or not isinstance(number, (int, float)):
                    raise ValueError(f"{field} must be a nonnegative finite number")
                if not math.isfinite(number) or number < 0:
                    raise ValueError(f"{field} must be a nonnegative finite number")
            numbers[field] = number
        error = value.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("reading error must be text or null")
        return cls(level, **numbers, error=error)


def parse_macos(level_text: str, swap_text: str, vm_text: str, now: float) -> Reading:
    try:
        level = int(level_text.strip())
    except ValueError as exc:
        raise ValueError("macOS returned an invalid memory-pressure level") from exc
    if level not in (1, 2, 4):
        raise ValueError(f"unsupported memory-pressure level: {level}")
    swap = re.search(r"used\s*=\s*([\d.]+)([MG])", swap_text)
    pagesize = re.search(r"page size of (\d+) bytes", vm_text)
    compressed = re.search(r"Pages occupied by compressor:\s*(\d+)\.", vm_text)
    if not swap or not pagesize or not compressed:
        raise ValueError("macOS memory statistics were incomplete")
    swap_mb = float(swap[1]) * (1024 if swap[2] == "G" else 1)
    compressor_mb = int(compressed[1]) * int(pagesize[1]) / (1024 * 1024)
    return Reading.from_dict({"level": level, "swap_mb": swap_mb,
                              "compressor_mb": compressor_mb, "sampled_at": now})


def command_budget(deadline: float | None = None) -> float:
    remaining = 2.0 if deadline is None else min(2.0, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("admission deadline reached")
    return remaining


def probe(fixture: Path | None = None, deadline: float | None = None) -> Reading:
    """Return an explicit unavailable reading when a probe cannot be trusted."""
    now = time.time()
    try:
        if fixture is not None:
            command_budget(deadline)
            descriptor = os.open(fixture, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    raise ValueError("probe fixture must be a regular JSON file of at most 64 KiB")
                contents = handle.read(65537)
                if len(contents) > 65536:
                    raise ValueError("probe fixture exceeds 64 KiB")
            value = json.loads(contents)
            value["sampled_at"] = now
            return Reading.from_dict(value)
        if sys.platform != "darwin":
            return Reading(None, sampled_at=now, error="live memory probing requires macOS")
        output = []
        for command in (("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"),
                        ("/usr/sbin/sysctl", "-n", "vm.swapusage"), ("/usr/bin/vm_stat",)):
            result = subprocess.run(command, capture_output=True, text=True, timeout=command_budget(deadline),
                                    check=True, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
            output.append(result.stdout)
        return parse_macos(*output, now)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        return Reading(None, sampled_at=now, error=str(exc))


def admission_limit(current: Reading, previous: Reading | None, jobs: int,
                    warning_jobs: int, swap_rise: float, compressor_rise: float) -> tuple[int, str]:
    if current.level is None:
        return 0, "memory reading unavailable"
    if current.level == 4:
        return 0, "critical memory pressure"
    if previous and previous.level is not None:
        elapsed = current.sampled_at - previous.sampled_at
        if 0 < elapsed <= 120:
            for field, maximum in (("swap_mb", swap_rise), ("compressor_mb", compressor_rise)):
                before, after = getattr(previous, field), getattr(current, field)
                if before is not None and after is not None and (after - before) / elapsed > maximum:
                    return 0, f"{field.removesuffix('_mb')} rising faster than {maximum:g} MB/s"
    if current.level == 2:
        return min(jobs, warning_jobs), "warning memory pressure"
    return jobs, "normal memory pressure"
