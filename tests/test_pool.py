import json
import os
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest.mock import patch

from memory_runner.pool import Policy, Pool, PoolError
from memory_runner.pressure import probe


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "pool"
        self.fixture = Path(self.temp.name) / "pressure.json"
        self.fixture.write_text('{"level":1,"swap_mb":0,"compressor_mb":0}')
        self.pool = Pool(self.directory, Policy(), .01, self.fixture)

    def test_pool_requires_private_directory(self):
        self.directory.chmod(0o755)
        with self.assertRaises(PoolError):
            Pool(self.directory, Policy())

    def test_state_symlink_is_refused_including_dangling_target(self):
        self.pool.state_path.symlink_to(Path(self.temp.name) / "nonexistent")
        with self.assertRaises(PoolError):
            self.pool.status()

    def test_corrupted_state_is_preserved_and_refused(self):
        for text in ('{', '{"version":1,"queue":[],"running":[]}',
                     '{"version":2,"queue":[],"running":[]}'):
            self.pool.state_path.write_text(text)
            with self.assertRaises(PoolError):
                self.pool.status()
            self.assertEqual(self.pool.state_path.read_text(), text)

    def test_live_slot_remains_and_dead_or_reused_pid_is_pruned(self):
        self.assertTrue(self.pool.acquire("test", 1)[0])
        state = self.pool.status()
        self.assertEqual(state["running"][0]["pid"], os.getpid())
        state["running"][0]["identity"] = "a different process start"
        self.pool._write(state)
        self.assertEqual(self.pool.status()["running"], [])

    def test_unavailable_process_lookup_does_not_erase_ownership(self):
        self.pool.acquire("test", 1)
        before = self.pool.state_path.read_bytes()
        with patch("memory_runner.pool.process_identity", side_effect=PoolError("probe failed")):
            with self.assertRaises(PoolError):
                self.pool.status()
        self.assertEqual(self.pool.state_path.read_bytes(), before)

    def test_release_is_idempotent(self):
        self.pool.acquire("test", 1)
        self.pool.release()
        self.pool.release()
        self.assertEqual(self.pool.status()["running"], [])

    def test_active_policy_mismatch_is_refused(self):
        self.pool.acquire("test", 1)
        other = Pool(self.directory, Policy(jobs=3), .01, self.fixture)
        with self.assertRaises(PoolError):
            other.acquire("other", .01)
        self.pool.release()
        self.assertTrue(other.acquire("other", 1)[0])
        other.release()

    def test_slow_probe_cannot_admit_after_deadline(self):
        def slow_probe(fixture, deadline=None):
            time.sleep(.06)
            return probe(fixture)
        with patch("memory_runner.pool.probe", side_effect=slow_probe):
            admitted, reason = self.pool.acquire("expired", .02)
        self.assertFalse(admitted)
        self.assertIn("deadline", reason)
        state = self.pool.status()
        self.assertEqual(state["running"], [])
        self.assertEqual(state["queue"], [])

    def test_process_lookup_is_bounded_by_admission_deadline(self):
        self.pool.acquire("live", 1)
        other = Pool(self.directory, Policy(), .01, self.fixture)
        seen = []
        def slow_lookup(command, **kwargs):
            seen.append(kwargs["timeout"])
            time.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        with patch("memory_runner.pool.subprocess.run", side_effect=slow_lookup):
            admitted, reason = other.acquire("waiting", .03)
        self.assertFalse(admitted)
        self.assertIn("deadline", reason)
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], .03)
        self.assertEqual(len(self.pool.status()["running"]), 1)
