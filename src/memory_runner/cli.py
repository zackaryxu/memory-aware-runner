"""Command-line entry points."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import sys

from . import __version__
from .pool import Policy, Pool, PoolError, default_pool
from .pressure import probe


def positive_integer(text: str) -> int:
    try:
        value = int(text)
        if value > 0:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def finite_number(text: str) -> float:
    try:
        value = float(text)
        if math.isfinite(value) and value >= 0:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a nonnegative finite number")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Run commands within a shared memory-aware concurrency limit.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="action", required=True)
    status = commands.add_parser("status", help="show current memory readings and pool entries")
    status.add_argument("--pool", type=Path, default=default_pool())
    status.add_argument("--json", action="store_true")
    status.add_argument("--probe-file", type=Path, help="read an explicit simulated memory reading")
    run = commands.add_parser("run", help="wait for admission, then replace this process with the command")
    run.add_argument("--pool", type=Path, default=default_pool())
    run.add_argument("--jobs", type=positive_integer, default=2)
    run.add_argument("--warning-jobs", type=positive_integer, default=1)
    run.add_argument("--poll", type=finite_number, default=2.0, metavar="SECONDS")
    run.add_argument("--wait-timeout", type=finite_number, default=300.0, metavar="SECONDS",
                     help="maximum admission wait; 0 waits indefinitely (default 300)")
    run.add_argument("--swap-rise", type=finite_number, default=25.0, metavar="MB_PER_SECOND")
    run.add_argument("--compressor-rise", type=finite_number, default=50.0, metavar="MB_PER_SECOND")
    run.add_argument("--probe-file", type=Path, help="use a simulated reading; never mix with a live pool")
    run.add_argument("--label", help="short label stored in pool status; defaults to executable basename")
    run.add_argument("command", nargs=argparse.REMAINDER)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    try:
        if args.action == "status":
            pool = Pool(args.pool, Policy())
            state = pool.status()
            stored_fixture = Path(state["fixture"]) if state.get("fixture") else None
            fixture = args.probe_file.resolve() if args.probe_file else stored_fixture
            if (args.probe_file and fixture != stored_fixture and (state["running"] or state["queue"])):
                arguments.error("--probe-file must match the active pool's pressure source")
            source = str(fixture) if fixture else "live macOS"
            reading = probe(fixture)
            result = {"pool": str(pool.directory), "policy": state["policy"],
                      "running": state["running"], "queue": state["queue"],
                      "memory_source": source, "memory": reading.to_dict()}
            if args.json:
                print(json.dumps(result, allow_nan=False, indent=2))
            else:
                names = {None: "unavailable", 1: "normal", 2: "warning", 4: "critical"}
                print(f"Memory: {names[reading.level]}")
                print(f"Source: {source}")
                if reading.error:
                    print(f"Probe: {reading.error}")
                print(f"Jobs: {len(state['running'])} running, {len(state['queue'])} queued")
                print(f"Pool: {pool.directory}")
                for kind in ("running", "queue"):
                    for entry in state[kind]:
                        print(f"  {kind}: PID {entry['pid']} {entry['label']}")
            return 0 if reading.level is not None else 2
        if args.poll <= 0:
            arguments.error("--poll must be greater than zero")
        if args.warning_jobs > args.jobs:
            arguments.error("--warning-jobs must not exceed --jobs")
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            arguments.error("run requires a command after --")
        policy = Policy(args.jobs, args.warning_jobs, args.swap_rise, args.compressor_rise)
        pool = Pool(args.pool, policy, args.poll, args.probe_file)
        def interrupted(number, frame):
            raise KeyboardInterrupt(number)
        previous = {number: signal.signal(number, interrupted) for number in (signal.SIGINT, signal.SIGTERM)}
        exec_signals = {}
        acquired = False
        try:
            acquired, reason = pool.acquire(args.label or Path(command[0]).name, args.wait_timeout)
            if not acquired:
                print(f"memory-runner: admission timed out ({reason})", file=sys.stderr)
                return 75
            # exec retains this PID and start identity. The slot therefore tracks the
            # actual workload, including when the original command name changes.
            for number, handler in previous.items():
                signal.signal(number, handler)
            # Python ignores these signals at startup; native commands expect
            # the ordinary shell dispositions, including SIGPIPE in pipelines.
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                number = getattr(signal, name, None)
                if number is not None:
                    exec_signals[number] = signal.signal(number, signal.SIG_DFL)
            os.execvpe(command[0], command, os.environ.copy())
        except KeyboardInterrupt as exc:
            return 128 + (exc.args[0] if exc.args else signal.SIGINT)
        finally:
            # Successful exec never returns. The next pool operation removes a slot
            # once its process exits; failed exec and interrupted waits release here.
            if not acquired or sys.exc_info()[0] is not None:
                pool.release(wait=False)
            for number, handler in exec_signals.items():
                signal.signal(number, handler)
            for number, handler in previous.items():
                signal.signal(number, handler)
    except (PoolError, OSError, ValueError) as exc:
        print(f"memory-runner: {exc}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else 2
    return 0
