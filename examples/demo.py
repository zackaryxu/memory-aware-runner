"""Demonstrate a critical-pressure hold followed by a two-job admission limit."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    with tempfile.TemporaryDirectory(prefix="memory-runner-demo-") as directory:
        root = Path(directory)
        probe = root / "pressure.json"
        events = root / "events.jsonl"
        probe.write_text('{"level":4,"swap_mb":0,"compressor_mb":0}')
        def recover():
            replacement = root / "next.json"
            replacement.write_text('{"level":1,"swap_mb":0,"compressor_mb":0}')
            replacement.replace(probe)
        jobs = []
        try:
            for _ in range(5):
                jobs.append(subprocess.Popen([sys.executable, "-m", "memory_runner", "run",
                    "--pool", str(root / "pool"), "--probe-file", str(probe), "--jobs", "2",
                    "--poll", ".05", "--wait-timeout", "10", "--", sys.executable,
                    str(Path(__file__).with_name("work.py")), str(events)]))
            deadline = time.monotonic() + 8
            while True:
                state_path = root / "pool" / "state.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                if len(state.get("queue", [])) == len(jobs):
                    if state.get("running") or events.exists():
                        raise SystemExit("a demonstration job started during critical pressure")
                    break
                if time.monotonic() >= deadline or any(job.poll() is not None for job in jobs):
                    raise SystemExit("demonstration jobs did not enter the pressure hold")
                time.sleep(0.02)
            recover()
            results = [job.wait(timeout=15) for job in jobs]
        finally:
            for job in jobs:
                if job.poll() is None:
                    job.terminate()
                    try:
                        job.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        job.kill()
                        job.wait()
        if any(results):
            raise SystemExit(f"demonstration jobs failed: {results}")
        active = set()
        peak = 0
        for row in sorted(map(json.loads, events.read_text().splitlines()), key=lambda row: row["time"]):
            if row["kind"] == "start":
                active.add(row["pid"])
            else:
                active.remove(row["pid"])
            peak = max(peak, len(active))
        if peak != 2 or active:
            raise SystemExit("demonstration exceeded its expected concurrency")
        print(json.dumps({"jobs_completed": len(jobs), "peak_concurrent_jobs": peak,
                          "jobs_held_during_critical_pressure": len(jobs),
                          "pressure_source": "simulated critical, then normal"}, indent=2))


if __name__ == "__main__":
    main()
