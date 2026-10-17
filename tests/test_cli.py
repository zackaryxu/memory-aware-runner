import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = self.root / "pool"
        self.probe = self.root / "probe.json"
        self.write_probe(1)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()

    def write_probe(self, level):
        tmp = self.root / "next.json"
        tmp.write_text(json.dumps({"level": level, "swap_mb": 0, "compressor_mb": 0}))
        tmp.replace(self.probe)

    def command(self, code, *options):
        return [sys.executable, "-m", "memory_runner", "run", "--pool", str(self.pool),
                "--probe-file", str(self.probe), "--poll", ".03", "--wait-timeout", "5",
                *options, "--", sys.executable, "-c", code]

    def spawn(self, code, *options):
        child = subprocess.Popen(self.command(code, *options), env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def run_cli(self, arguments):
        return subprocess.run([sys.executable, "-m", "memory_runner", *arguments],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def wait_state(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = json.loads((self.pool / "state.json").read_text())
                if predicate(state):
                    return state
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(.01)
        self.fail("pool did not reach expected state")

    def test_command_output_exit_status_and_pid_are_preserved(self):
        child = self.spawn('import os,sys; print(os.getpid());sys.exit(7)')
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 7, err)
        self.assertEqual(int(out), child.pid)

    def test_missing_executable_releases_slot(self):
        result = self.run_cli(["run", "--pool", str(self.pool), "--probe-file", str(self.probe),
                               "--", "/does/not/exist"])
        self.assertEqual(result.returncode, 127, result.stderr)
        state = json.loads((self.pool / "state.json").read_text())
        self.assertEqual(state["running"], [])

    def test_multiple_processes_obey_shared_limit(self):
        events = self.root / "events.jsonl"
        code = ("import os,json,time; p=" + repr(str(events)) + "; "
              "f=os.open(p,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600); "
              "os.write(f,(json.dumps({'kind':'start','pid':os.getpid(),'at':time.time()})+'\\n').encode()); "
              "time.sleep(.22); "
              "os.write(f,(json.dumps({'kind':'end','pid':os.getpid(),'at':time.time()})+'\\n').encode());os.close(f)")
        jobs = [self.spawn(code) for _ in range(5)]
        for job in jobs:
            _, err = job.communicate(timeout=10)
            self.assertEqual(job.returncode, 0, err)
        live = set()
        maximum = 0
        for event in sorted(map(json.loads, events.read_text().splitlines()), key=lambda e: e["at"]):
            if event["kind"] == "start":
                live.add(event["pid"])
            else:
                live.remove(event["pid"])
            maximum = max(maximum, len(live))
        self.assertEqual(maximum, 2)
        self.assertEqual(live, set())

    def test_critical_hold_resumes_on_recovery(self):
        self.write_probe(4)
        marker = self.root / "ran"
        child = self.spawn("from pathlib import Path;Path(" + repr(str(marker)) + ").touch()")
        self.wait_state(lambda s: len(s["queue"]) == 1)
        self.assertFalse(marker.exists())
        self.write_probe(1)
        _, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertTrue(marker.exists())

    def test_unavailable_probe_times_out_without_launching(self):
        self.probe.write_text('{"level":null}')
        child = self.spawn('raise RuntimeError("must not run")', "--wait-timeout", ".12")
        _, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 75, err)
        self.assertIn("unavailable", err)
        state = json.loads((self.pool / "state.json").read_text())
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["running"], [])

    def test_future_cached_healthy_reading_does_not_bypass_critical_pressure(self):
        seed = self.spawn('pass')
        seed.communicate(timeout=5)
        self.assertEqual(seed.returncode, 0)
        state_path = self.pool / "state.json"
        state = json.loads(state_path.read_text())
        state["reading"]["sampled_at"] = time.time() + 3600
        state_path.write_text(json.dumps(state))
        self.write_probe(4)
        child = self.spawn('raise RuntimeError("must not run")', "--wait-timeout", ".15")
        _, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 75, err)
        self.assertIn("critical", err)

    def test_admission_deadline_expires_while_pool_lock_is_held(self):
        self.pool.mkdir(mode=0o700)
        with (self.pool / "pool.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            child = self.spawn('raise RuntimeError("must not run")', "--wait-timeout", ".1")
            _, err = child.communicate(timeout=2)
            self.assertEqual(child.returncode, 75, err)
            self.assertIn("pool lock", err)
            self.assertFalse((self.pool / "state.json").exists())

    def test_queued_ticket_expires_without_waiting_for_cleanup_lock(self):
        self.write_probe(4)
        child = self.spawn('raise RuntimeError("must not run")', "--wait-timeout", ".3")
        self.wait_state(lambda s: len(s["queue"]) == 1)
        with (self.pool / "pool.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.write_probe(1)
            _, err = child.communicate(timeout=2)
            self.assertEqual(child.returncode, 75, err)
        result = self.run_cli(["status", "--pool", str(self.pool), "--probe-file", str(self.probe), "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["queue"], [])

    def test_special_file_probe_fails_closed_and_does_not_hold_pool_lock(self):
        self.probe.unlink()
        os.mkfifo(self.probe)
        child = self.spawn('raise RuntimeError("must not run")', "--wait-timeout", ".1")
        _, err = child.communicate(timeout=2)
        self.assertEqual(child.returncode, 75, err)
        self.probe.unlink()
        self.write_probe(1)
        next_job = self.spawn('print("recovered")')
        out, err = next_job.communicate(timeout=2)
        self.assertEqual(next_job.returncode, 0, err)
        self.assertEqual(out.strip(), "recovered")

    def test_status_uses_active_pool_pressure_source(self):
        self.write_probe(4)
        child = self.spawn('pass')
        self.wait_state(lambda s: len(s["queue"]) == 1)
        result = self.run_cli(["status", "--pool", str(self.pool), "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["memory"]["level"], 4)
        self.assertEqual(status["memory_source"], str(self.probe.resolve()))
        self.write_probe(1)
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0)

    def test_each_admission_probes_again_even_with_a_long_poll_interval(self):
        for change in ("critical", "missing"):
            self.write_probe(1)
            seed = self.spawn('pass', "--poll", "60")
            seed.communicate(timeout=5)
            self.assertEqual(seed.returncode, 0)
            if change == "critical":
                self.write_probe(4)
            else:
                self.probe.unlink()
            child = self.spawn('raise RuntimeError("must not run")',
                               "--poll", "60", "--wait-timeout", ".15")
            _, err = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 75, err)

    def test_native_pipeline_writer_receives_default_sigpipe(self):
        command = self.command('pass')
        command = command[:command.index("--") + 1] + ["/usr/bin/yes"]
        child = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertTrue(child.stdout.readline())
        child.stdout.close()
        child.stdout = None
        _, err = child.communicate(timeout=3)
        self.assertEqual(child.returncode, -signal.SIGPIPE, err)
        self.assertEqual(err, "")

    def test_interrupted_wait_removes_ticket(self):
        self.write_probe(4)
        child = self.spawn('pass')
        self.wait_state(lambda s: len(s["queue"]) == 1)
        child.send_signal(signal.SIGTERM)
        _, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 143, err)
        self.assertEqual(json.loads((self.pool / "state.json").read_text())["queue"], [])

    def test_running_process_survives_wrapper_replacement_and_slot_is_pruned_after_exit(self):
        child = self.spawn('import time;time.sleep(5)')
        self.wait_state(lambda s: len(s["running"]) == 1)
        child.terminate()
        child.communicate(timeout=5)
        result = self.run_cli(["status", "--pool", str(self.pool), "--probe-file", str(self.probe), "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["running"], [])

    def test_invalid_arguments_do_not_create_pool(self):
        for args in (["--poll", "nan"], ["--poll", "0"], ["--jobs", "0"],
                     ["--jobs", "1", "--warning-jobs", "2"]):
            child = subprocess.run(self.command('pass', *args), env=self.env,
                                   capture_output=True, text=True)
            self.assertEqual(child.returncode, 2, child.stderr)
            self.assertFalse(self.pool.exists())
