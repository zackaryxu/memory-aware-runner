"""A per-user FIFO admission pool protected by a POSIX advisory lock."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from .pressure import Reading, admission_limit, command_budget, probe


class PoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    jobs: int = 2
    warning_jobs: int = 1
    swap_rise: float = 25.0
    compressor_rise: float = 50.0


def default_pool() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MemoryAwareRunner"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "memory-runner"


def process_identity(pid: int, deadline: float | None = None) -> str | None:
    """Include process start time so an unrelated reused PID cannot retain a slot."""
    try:
        budget = command_budget(deadline)
        if sys.platform.startswith("linux"):
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return None if fields[0] == "Z" else fields[19]
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
                                capture_output=True, text=True, timeout=budget,
                                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        text = result.stdout.strip()
        if result.returncode == 1 and not text:
            return None
        if result.returncode or not text:
            raise PoolError(f"process lookup failed for PID {pid}")
        state, started = text.split(maxsplit=1)
        return None if "Z" in state else started
    except TimeoutError:
        raise
    except subprocess.TimeoutExpired as exc:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("process lookup exceeded the admission deadline") from exc
        raise PoolError(f"process lookup timed out for PID {pid}") from exc
    except FileNotFoundError:
        if sys.platform.startswith("linux"):
            return None
        raise PoolError("process lookup command is unavailable")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise PoolError(f"cannot establish process identity for PID {pid}: {exc}") from exc


class Pool:
    def __init__(self, directory: Path, policy: Policy, interval: float = 2.0,
                 fixture: Path | None = None):
        self.directory = directory.expanduser().absolute()
        self.policy = policy
        self.interval = interval
        self.fixture = fixture.resolve() if fixture else None
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PoolError("pool must be a private directory owned by the current user (mode 700)")
        self.state_path = self.directory / "state.json"
        self.lock_path = self.directory / "pool.lock"
        self.ticket = uuid.uuid4().hex
        self.pid = os.getpid()
        self.identity = process_identity(self.pid)
        if self.identity is None:
            raise PoolError("cannot identify the current process")

    @contextmanager
    def locked(self, deadline: float | None = None):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            if deadline is None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("pool lock was busy until the admission deadline")
                        time.sleep(min(0.01, remaining))
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> dict:
        if self.state_path.is_symlink():
            raise PoolError("pool state must not be a symbolic link")
        if not self.state_path.exists():
            return {"version": 1, "policy": asdict(self.policy), "queue": [], "running": [],
                    "reading": None, "previous": None, "fixture": str(self.fixture) if self.fixture else None}
        try:
            descriptor = os.open(self.state_path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("pool state must be a regular JSON file")
                value = json.load(handle)
            required = {"version", "policy", "queue", "running", "reading", "previous", "fixture"}
            if (not isinstance(value, dict) or not required <= value.keys()
                    or type(value["version"]) is not int or value["version"] != 1
                    or not isinstance(value["queue"], list) or not isinstance(value["running"], list)):
                raise ValueError("unsupported state format")
            for entry in value["queue"] + value["running"]:
                if (not isinstance(entry, dict) or not isinstance(entry["pid"], int)
                        or entry["pid"] <= 0 or not isinstance(entry["identity"], str)
                        or not isinstance(entry["ticket"], str)):
                    raise ValueError("invalid pool entry")
                if not isinstance(entry.get("label"), str):
                    raise ValueError("invalid pool label")
            policy = value["policy"]
            if (set(policy) != {"jobs", "warning_jobs", "swap_rise", "compressor_rise"}
                    or type(policy["jobs"]) is not int or policy["jobs"] < 1
                    or type(policy["warning_jobs"]) is not int
                    or not 1 <= policy["warning_jobs"] <= policy["jobs"]):
                raise ValueError("invalid pool policy")
            for key in ("swap_rise", "compressor_rise"):
                number = policy[key]
                if type(number) not in (int, float) or not 0 <= number < float("inf"):
                    raise ValueError("invalid pressure threshold")
            for key in ("reading", "previous"):
                if value.get(key) is not None:
                    Reading.from_dict(value[key])
            if value.get("fixture") is not None and not isinstance(value["fixture"], str):
                raise ValueError("invalid probe source")
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PoolError(f"cannot read pool state: {exc}; keep it for inspection instead of resetting it") from exc

    def _write(self, value: dict):
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, allow_nan=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _prune(self, state: dict, deadline: float | None = None):
        identities = {}
        for kind in ("queue", "running"):
            retained = []
            for entry in state[kind]:
                pid = entry["pid"]
                if pid not in identities:
                    identities[pid] = process_identity(pid, deadline=deadline)
                if identities[pid] == entry["identity"]:
                    retained.append(entry)
            state[kind] = retained

    def _prepare(self, state: dict, deadline: float | None = None):
        self._prune(state, deadline)
        expected = asdict(self.policy)
        fixture = str(self.fixture) if self.fixture else None
        if state["policy"] != expected or state.get("fixture") != fixture:
            if state["queue"] or state["running"]:
                raise PoolError("active pool uses a different policy or probe source; use matching options or a separate --pool")
            state.update(policy=expected, fixture=fixture, reading=None, previous=None)

    def _sample(self, state: dict, deadline: float | None = None,
                force: bool = False) -> tuple[Reading, Reading | None]:
        current = Reading.from_dict(state["reading"]) if state.get("reading") else None
        age = time.time() - current.sampled_at if current else None
        if force or age is None or age < 0 or age >= self.interval:
            state["previous"] = current.to_dict() if current else None
            current = probe(self.fixture, deadline=deadline)
            state["reading"] = current.to_dict()
        previous = Reading.from_dict(state["previous"]) if state.get("previous") else None
        return current, previous

    def acquire(self, label: str, timeout: float) -> tuple[bool, str]:
        started = time.monotonic()
        deadline = started + timeout if timeout else None
        reason = "waiting"
        while True:
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(reason)
                with self.locked(deadline):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("pool lock wait exceeded the admission deadline")
                    state = self._read()
                    self._prepare(state, deadline)
                    if not any(entry["ticket"] == self.ticket for entry in state["queue"] + state["running"]):
                        state["queue"].append({"ticket": self.ticket, "pid": self.pid, "identity": self.identity,
                                               "label": label[:80], "queued_at": time.time()})
                    first = state["queue"] and state["queue"][0]["ticket"] == self.ticket
                    last_reason = reason
                    # Only the head can be admitted. It always obtains a fresh
                    # reading, independently of how long its poll interval is.
                    current, previous = self._sample(state, deadline, force=bool(first))
                    limit, reason = admission_limit(current, previous, **asdict(self.policy))
                    if deadline is not None and time.monotonic() >= deadline:
                        detail = last_reason if last_reason != "waiting" else "memory sampling"
                        raise TimeoutError(f"{detail}; admission deadline reached")
                    if first and len(state["running"]) < limit:
                        entry = state["queue"].pop(0)
                        entry["started_at"] = time.time()
                        state["running"].append(entry)
                        self._write(state)
                        return True, reason
                    self._write(state)
            except TimeoutError as exc:
                # Do not turn a bounded admission wait into an unbounded cleanup
                # wait. A busy pool will prune this process's ticket after exit.
                self.release(wait=False)
                detail = str(exc)
                if reason != "waiting" and reason not in detail:
                    detail = f"{reason}; {detail}"
                return False, detail
            remaining = deadline - time.monotonic() if deadline is not None else self.interval
            if remaining > 0:
                time.sleep(min(self.interval, remaining))

    def release(self, wait: bool = True):
        try:
            with self.locked(None if wait else time.monotonic()):
                state = self._read()
                for kind in ("queue", "running"):
                    state[kind] = [entry for entry in state[kind] if entry["ticket"] != self.ticket]
                self._write(state)
        except TimeoutError:
            return False
        return True

    def status(self) -> dict:
        with self.locked():
            state = self._read()
            self._prune(state)
            # Inspect an active pool using its own policy and sample source.
            self._write(state)
            return state
