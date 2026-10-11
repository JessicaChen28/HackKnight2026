"""Retry invalid worlds, but never hide environmental failures or loop forever."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import generate_world as generator


class GenerationRetryTests(unittest.TestCase):
    def run_attempts(self, max_attempts=5):
        with TemporaryDirectory() as root:
            workspace = Path(root) / "stage"
            workspace.mkdir()
            return generator._generate_with_retries(
                {"max_attempts": max_attempts, "timeout_seconds": 10, "export_root": root},
                workspace, lambda *args: None)

    def test_invalid_world_is_replaced_and_success_returned(self):
        result = Path("valid-world/azgaar_minimal.json")
        with patch.object(generator, "_generate_world_once", side_effect=[generator.WorldProcessingError("off land"), result]) as run:
            self.assertEqual(self.run_attempts(max_attempts=3), result)
            self.assertEqual(run.call_count, 2)

    def test_attempt_limit_is_enforced(self):
        with patch.object(generator, "_generate_world_once", side_effect=generator.WorldProcessingError("invalid coast")) as run:
            with self.assertRaisesRegex(generator.WorldProcessingError, "No valid world after 2 attempts"):
                self.run_attempts(max_attempts=2)
            self.assertEqual(run.call_count, 2)

    def test_disk_failures_are_not_retried(self):
        with patch.object(generator, "_generate_world_once", side_effect=PermissionError("no write access")) as run:
            with self.assertRaises(PermissionError):
                self.run_attempts()
            self.assertEqual(run.call_count, 1)

    def test_invalid_arguments_do_not_launch_browser(self):
        with patch.object(generator, "_generate_world_once") as run:
            for kwargs in ({"max_attempts": 0}, {"coastline_tolerance": -1}, {"coastline_tolerance": float("nan")}):
                with self.assertRaises(ValueError):
                    generator.generate_world(**kwargs)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
