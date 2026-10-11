"""Offline malformed-input, filesystem, and failure-recovery tests."""

import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock, patch

import generate_world as generator
import safe_storage as storage
import world_parser as wp
from generation_runtime import ACTIVE_GENERATION, GenerationCancelled, browser_environment


def minimal():
    return {"info": {"mapName": "Test", "version": "1", "seed": "123", "width": 100, "height": 100,
                     "description": "Azgaar Minimal export"},
            "pack": {"burgs": [0, {"i": 17, "name": "Town", "x": 50, "y": 50, "state": 0, "feature": 1}],
                     "states": [{"i": 0, "name": "Neutral"}],
                     "features": [0, {"i": 1, "land": True, "type": "island"}]}}


class StorageTests(unittest.TestCase):
    def test_names_cannot_be_paths_or_devices(self):
        values = ["../../other_directory", r"..\..\Windows\System32", "world/../../secrets",
                  "CON", "AUX", "COM1.txt", "LPT²", "", " . ", "x" * 500, "東京🌊" * 100,
                  "a\x00b\n\u202ec", "\ud83d"]
        for value in values:
            with self.subTest(value=ascii(value)):
                name = storage.safe_name(value)
                self.assertTrue(name)
                self.assertNotIn("..", name)
                self.assertFalse(any(c in name for c in '/\\<>:"|?*\x00\n'))
                self.assertLessEqual(len(name.encode("utf-8")), 166)
                self.assertEqual(name, name.rstrip(" ."))
        self.assertEqual(storage.safe_name("東京"), "東京")
        self.assertTrue(storage.safe_name("CON").startswith("world_"))

    def test_writes_and_directory_publication_never_overwrite(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "data.json"
            storage.write_json(file, {"old": True})
            with self.assertRaises(FileExistsError):
                storage.write_json(file, {"new": True})
            self.assertEqual(storage.read_json(file), {"old": True})
            self.assertFalse(list(root.glob("*.part")))
            destinations = []
            for _ in range(2):
                with storage.staging_directory(root) as stage:
                    destinations.append(storage.publish_world(stage, root, "../../CON"))
            self.assertNotEqual(*destinations)
            for dest in destinations:
                self.assertEqual(dest.parent, root)
            source, existing = root / "source", root / "existing"
            source.mkdir()
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                storage.rename_directory(source, existing)
            self.assertTrue(source.exists())
            self.assertTrue(existing.exists())

    def test_containment_and_staging_cleanup(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                storage.checked_path(root / ".." / "escape", root)
            with self.assertRaises(RuntimeError):
                with storage.staging_directory(root) as stage:
                    (stage / "partial").write_text("partial")
                    raise RuntimeError("interruption")
            self.assertFalse(stage.exists())
            self.assertTrue(root.exists())

    def test_symlink_or_windows_junction_is_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            target, link = root / "target", root / "link"
            target.mkdir()
            if os.name == "nt":
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                link.symlink_to(target, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, "links|reparse"):
                    storage.prepare_root(link / "child")
                self.assertFalse((target / "child").exists())
            finally:
                if os.name == "nt":
                    link.rmdir()  # removes only this tested junction, not its target
                else:
                    link.unlink()

    def test_kernel_lock_releases_after_exception(self):
        with TemporaryDirectory() as temp:
            with storage.generation_lock(temp):
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    with storage.generation_lock(temp):
                        self.fail("second lock acquired")
            with storage.generation_lock(temp):
                pass

    def test_invalid_json_and_limits(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            for raw in (b"", b"{", b'{"x": NaN}', b'{"x": 1e999}', b'{"x":1,"x":2}',
                        b"[" * 150 + b"0" + b"]" * 150, b"\xff"):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                    storage.read_json(path)
            path.write_text('{"valid":true}')
            with self.assertRaises(ValueError):
                storage.read_json(path, 4)
            self.assertEqual(storage.read_json(path), {"valid": True})

    def test_interrupted_serialization_never_publishes_partial_json(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "output.json"

            def interrupted(data, stream, **kwargs):
                stream.write('{"partial":')
                raise OSError("disk full")

            with patch.object(storage.json, "dump", side_effect=interrupted), self.assertRaises(OSError):
                storage.write_json(path, {"valid": True})
            self.assertFalse(list(Path(temp).iterdir()))


class InputTests(unittest.TestCase):
    def test_optional_fields_and_ids(self):
        world = wp.parse_world(minimal())
        self.assertEqual(world["pack"]["burgs"][0]["i"], 17)
        self.assertEqual(world["pack"]["cultures"], [])
        self.assertNotIn("population", world["pack"]["burgs"][0])

    def test_missing_required_fields(self):
        for container, key in (("info", "mapName"), ("info", "seed"), ("info", "width"),
                               ("pack", "states"), ("pack", "features"), ("pack", "burgs")):
            data = minimal()
            del data[container][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                wp.parse_world(data)

    def test_corrupt_coordinates_ids_references_and_types(self):
        for field, value in (("x", float("nan")), ("x", float("inf")), ("x", 101), ("y", -1),
                             ("x", True), ("x", 10 ** 500), ("state", 999), ("feature", 999),
                             ("culture", 999), ("population", -1), ("i", True), ("name", [])):
            data = minimal()
            data["pack"]["burgs"][1][field] = value
            with self.subTest(field=field, value=str(value)[:25]), self.assertRaises(ValueError):
                wp.parse_world(data)
        data = minimal()
        data["pack"]["burgs"].append(copy.deepcopy(data["pack"]["burgs"][1]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            wp.parse_world(data)

    def test_output_failure_rolls_back_without_touching_raw_or_existing_world(self):
        with gzip.open(Path(__file__).parent / "fixtures/frepoyland.json.gz", "rt", encoding="utf8") as stream:
            fixture = json.load(stream)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "azgaar_minimal.json"
            storage.write_json(raw, fixture["minimal"])
            old_world = root / "previous-world.json"
            storage.write_json(old_world, {"existing": True})
            original = raw.read_bytes()

            def fail_coast(path, data):
                if path.name == "coastlines.json":
                    raise OSError("simulated disk full")
                return storage.write_json(path, data)

            with patch.object(wp, "write_json", side_effect=fail_coast):
                with self.assertRaises(OSError):
                    wp.process_world(raw, fixture["geometry_source"])
            self.assertEqual(raw.read_bytes(), original)
            self.assertEqual(storage.read_json(old_world), {"existing": True})
            self.assertEqual({p.name for p in root.iterdir()}, {raw.name, old_world.name})


class BrowserFailureTests(unittest.TestCase):
    def test_cli_reports_missing_dependencies_without_traceback(self):
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(Path(generator.__file__))],
            cwd=Path(generator.__file__).parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("python -m pip install -r requirements.txt", result.stderr)
        self.assertIn("psutil", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_worker_failure_removes_staging_and_preserves_previous_world(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            previous = root / "previous"
            previous.mkdir()
            storage.write_json(previous / "world.json", {"old": True})

            def failure(config, workspace, *args):
                (workspace / "partial").write_text("partial")
                raise generator.GenerationError("failed extraction")

            with patch.object(generator, "run_worker", side_effect=failure), self.assertRaisesRegex(RuntimeError, "extraction"):
                generator.generate_world(export_root=root)
            self.assertFalse(list(root.glob(".staging-*")))
            self.assertEqual(storage.read_json(previous / "world.json"), {"old": True})
            self.assertFalse(ACTIVE_GENERATION.locked())

    def test_navigation_policy_and_popups(self):
        context, page, popup = MagicMock(), MagicMock(), MagicMock()
        generator.guard_browser(context, page)
        route_handler = context.route.call_args.args[1]
        route = MagicMock()
        route.request.is_navigation_request.return_value = True
        for url in ("http://azgaar.github.io/Fantasy-Map-Generator/", "file:///C:/secret", "https://evil.test/",
                    "https://azgaar.github.io.evil.test/Fantasy-Map-Generator/", "https://azgaar.github.io/other/"):
            route.request.url = url
            route_handler(route)
        self.assertEqual(route.abort.call_count, 5)
        route.request.url = generator.URL
        route_handler(route)
        route.continue_.assert_called_once()
        route.request.is_navigation_request.return_value = False
        route.request.url = "https://cdn.example/script.js"
        route_handler(route)
        self.assertEqual(route.continue_.call_count, 2)
        context.on.call_args.args[1](popup)
        popup.close.assert_called_once()

    def test_environment_excludes_secrets(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret", "UNRELATED_TOKEN": "secret"}):
            env = browser_environment("test-temp")
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("UNRELATED_TOKEN", env)
        self.assertEqual(env["TEMP"], "test-temp")

    def test_cancellation_and_duplicate_ui_calls_do_not_start_worker(self):
        event = threading.Event()
        event.set()
        with patch.object(generator, "run_worker") as worker:
            with self.assertRaises(GenerationCancelled):
                generator.generate_world(cancel_event=event)
            with ACTIVE_GENERATION:
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    generator.generate_world()
            worker.assert_not_called()

    def test_browser_cleanup_after_download_timeout_extraction_and_parser_failures(self):
        for failure in ("download", "generation", "extraction", "parser"):
            with self.subTest(failure=failure), TemporaryDirectory() as temp:
                root = Path(temp)
                stage = root / "stage"
                stage.mkdir()
                config = dict(timeout_seconds=10, navigation_timeout_seconds=10, export_timeout_seconds=10,
                              geometry_timeout_seconds=10, headless=True, max_input_bytes=100000,
                              coastline_tolerance=3, keep_geometry_source=False)
                driver = MagicMock()
                browser = driver.chromium.launch.return_value
                context = browser.new_context.return_value
                page = context.new_page.return_value
                page.url = generator.URL
                page.locator.return_value.count.return_value = 0
                page.evaluate.return_value = {"seed": "123"}
                download = page.expect_download.return_value.__enter__.return_value.value
                download.failure.return_value = "cancelled" if failure == "download" else None
                payload = root / "payload"
                storage.write_json(payload, minimal())
                download.path.return_value = payload
                download.save_as.side_effect = lambda target: Path(target).write_bytes(payload.read_bytes())
                if failure == "generation":
                    page.wait_for_function.side_effect = RuntimeError("generation timeout")
                with patch.object(generator, "sync_playwright") as playwright, \
                     patch.object(generator, "extract_geometry", side_effect=RuntimeError("extraction failed")
                                  if failure == "extraction" else None, return_value={}), \
                     patch.object(generator, "process_world", side_effect=ValueError("parser failed")):
                    playwright.return_value.__enter__.return_value = driver
                    with self.assertRaises(RuntimeError):
                        generator._generate_world_once(config, stage, lambda *a: None)
                context.close.assert_called_once()
                browser.close.assert_called_once()
                self.assertTrue(driver.chromium.launch.call_args.kwargs["chromium_sandbox"])
                self.assertFalse((stage / "complete.json").exists())
                self.assertFalse((stage / "download.part").exists())


if __name__ == "__main__":
    unittest.main()
