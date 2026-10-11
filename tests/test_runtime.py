"""Real spawned workers test deadlines, cancellation, and descendant cleanup."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest

import psutil

from generation_runtime import GenerationCancelled, GenerationError, GenerationTimeout, run_worker


def stalled_worker(connection, stop, config, workspace):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    connection.send(("progress", f"child:{child.pid}"))
    connection.send(("phase", ("stalled geometry", config.get("phase_seconds", 0.5))))
    time.sleep(60)


def successful_worker(connection, stop, config, workspace):
    connection.send(("result", workspace))
    connection.close()


def failed_worker(connection, stop, config, workspace):
    connection.send(("phase", ("test download", 5)))
    connection.send(("error", "Download failed"))
    connection.close()


class RuntimeTests(unittest.TestCase):
    def test_memory_budget_stops_worker(self):
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(GenerationError, "memory budget"):
                run_worker({"total_timeout_seconds": 10, "max_memory_mb": 1}, temp,
                           target=successful_worker)

    def test_success_and_error_are_reaped(self):
        with TemporaryDirectory() as temp:
            config = {"total_timeout_seconds": 10}
            self.assertEqual(run_worker(config, temp, target=successful_worker), Path(temp))
            with self.assertRaisesRegex(GenerationError, "test download: Download failed"):
                run_worker(config, temp, target=failed_worker)

    def exercise_stop(self, cancel=False):
        children = []
        event = threading.Event()

        def progress(message):
            if message.startswith("child:"):
                children.append(int(message.split(":")[1]))
                if cancel:
                    event.set()

        with TemporaryDirectory() as temp:
            start = time.monotonic()
            with self.assertRaises(GenerationCancelled if cancel else GenerationTimeout):
                run_worker({"total_timeout_seconds": 15, "phase_seconds": 5 if cancel else 0.5},
                           temp, event, progress, target=stalled_worker)
            self.assertLess(time.monotonic() - start, 14)
            self.assertTrue(children)
            self.assertTrue(all(not psutil.pid_exists(pid) for pid in children))

    def test_hard_phase_timeout_stops_worker_and_child(self):
        self.exercise_stop()

    def test_cancellation_stops_worker_and_child(self):
        self.exercise_stop(cancel=True)

    def test_total_timeout_also_bounds_workers(self):
        with TemporaryDirectory() as temp:
            with self.assertRaises(GenerationTimeout):
                run_worker({"total_timeout_seconds": 1, "phase_seconds": 60}, temp,
                           target=stalled_worker, progress=None)


if __name__ == "__main__":
    unittest.main()
