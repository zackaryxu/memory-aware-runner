import json
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest.mock import patch

from memory_runner.pressure import Reading, admission_limit, parse_macos, probe


class PressureTests(unittest.TestCase):
    def limit(self, current, previous=None, **overrides):
        return admission_limit(current, previous, **dict(jobs=4, warning_jobs=1,
                    swap_rise=25.0, compressor_rise=50.0, **overrides))

    def test_macos_page_size_and_swap_units(self):
        result = parse_macos("2\n", "total = 4.00G used = 1.50G free = 2.50G",
                             'Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
                             'Pages occupied by compressor: 65536.\n', 100.0)
        self.assertEqual(result, Reading(2, 1536.0, 1024.0, 100.0))

    def test_incomplete_or_unrecognized_probe_is_not_healthy(self):
        for level in ("garbage", "0", "3"):
            with self.assertRaises(ValueError):
                parse_macos(level, "", "", 1)
        with self.assertRaises(ValueError):
            parse_macos("1", "", "", 1)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reading.json"
            for text in ('{', '[]', '{"level": true}', '{"level":1.0}', '{"level":1,"swap_mb":NaN}'):
                path.write_text(text)
                self.assertIsNone(probe(path).level)

    def test_critical_and_unavailable_hold_all_new_jobs(self):
        for level in (4, None):
            self.assertEqual(self.limit(Reading(level))[0], 0)

    def test_warning_allows_bounded_progress(self):
        self.assertEqual(self.limit(Reading(2))[0], 1)

    def test_stale_high_swap_does_not_block_normal_pressure(self):
        previous = Reading(1, 100000, 30000, 100)
        current = Reading(1, 100000, 30000, 102)
        self.assertEqual(self.limit(current, previous)[0], 4)

    def test_swap_and_compression_growth_temporarily_hold(self):
        previous = Reading(1, 1000, 1000, 100)
        self.assertEqual(self.limit(Reading(1, 1100, 1000, 102), previous)[0], 0)
        self.assertEqual(self.limit(Reading(1, 1000, 1200, 102), previous)[0], 0)
        self.assertEqual(self.limit(Reading(1, 1100, 1000, 102), Reading(1, 1100, 1000, 101))[0], 4)

    def test_old_or_nonmonotonic_readings_do_not_create_spurious_rates(self):
        self.assertEqual(self.limit(Reading(1, 10000, 10000, 400), Reading(1, 0, 0, 100))[0], 4)
        self.assertEqual(self.limit(Reading(1, 10000, 10000, 100), Reading(1, 0, 0, 100))[0], 4)

    def test_fixture_has_current_sample_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reading.json"
            path.write_text(json.dumps({"level": 1, "swap_mb": 4, "compressor_mb": 2}))
            result = probe(path)
            self.assertEqual(result.level, 1)
            self.assertGreater(result.sampled_at, 1_000_000)

    def test_live_commands_share_remaining_admission_budget(self):
        calls = []
        def slow_command(command, **kwargs):
            calls.append(kwargs["timeout"])
            time.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        with patch("memory_runner.pressure.sys.platform", "darwin"), patch(
                "memory_runner.pressure.subprocess.run", side_effect=slow_command):
            reading = probe(deadline=time.monotonic() + .03)
        self.assertIsNone(reading.level)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], .03)
