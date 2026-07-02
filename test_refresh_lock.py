"""
test_refresh_lock.py
--------------------
Offline tests for refresh.py's single-instance lock, added after two concurrent
refreshes corrupted sca_events_clean.csv and got the IP rate-banned by Nominatim
(2026-07-01). No network; no pipeline steps run (refresh.main() is never called).

Run:
    python -m unittest test_refresh_lock -v
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import refresh


class TestPidRunning(unittest.TestCase):
    def test_own_pid_is_running(self):
        self.assertTrue(refresh._pid_running(os.getpid()))

    def test_nonpositive_pids_not_running(self):
        self.assertFalse(refresh._pid_running(0))
        self.assertFalse(refresh._pid_running(-5))


class TestLock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(refresh, "LOCK_FILE", self.tmp / "refresh.lock")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_acquire_writes_own_pid_and_release_removes_it(self):
        refresh.acquire_lock()
        self.assertEqual(refresh.LOCK_FILE.read_text(encoding="ascii").strip(),
                         str(os.getpid()))
        refresh.release_lock()
        self.assertFalse(refresh.LOCK_FILE.exists())

    def test_live_holder_blocks_a_second_instance(self):
        # Use our parent process as the "other refresh": it is genuinely alive,
        # so acquire must refuse and leave the holder's lock untouched.
        other = os.getppid()
        if not refresh._pid_running(other):
            self.skipTest("parent PID not visible — cannot simulate a live holder")
        refresh.LOCK_FILE.write_text(str(other), encoding="ascii")
        with self.assertRaises(SystemExit) as ctx:
            refresh.acquire_lock()
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(refresh.LOCK_FILE.read_text(encoding="ascii").strip(),
                         str(other))

    def test_stale_lock_from_dead_pid_is_reclaimed(self):
        refresh.LOCK_FILE.write_text("999999999", encoding="ascii")
        refresh.acquire_lock()
        self.assertEqual(refresh.LOCK_FILE.read_text(encoding="ascii").strip(),
                         str(os.getpid()))
        refresh.release_lock()

    def test_garbage_lock_is_reclaimed(self):
        refresh.LOCK_FILE.write_text("not-a-pid", encoding="ascii")
        refresh.acquire_lock()
        self.assertEqual(refresh.LOCK_FILE.read_text(encoding="ascii").strip(),
                         str(os.getpid()))
        refresh.release_lock()

    def test_release_is_a_noop_for_someone_elses_lock(self):
        refresh.LOCK_FILE.write_text("999999999", encoding="ascii")
        refresh.release_lock()   # not ours — must survive
        self.assertTrue(refresh.LOCK_FILE.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
