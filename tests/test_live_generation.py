"""Opt-in real browser integration: AZGAAR_LIVE_TEST=1, otherwise offline suite."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from generate_world import generate_world
from safe_storage import read_json
from world_parser import validate_outputs


@unittest.skipUnless(os.environ.get("AZGAAR_LIVE_TEST") == "1", "optional live Azgaar test")
class LiveGenerationTests(unittest.TestCase):
    def test_real_generation_and_atomic_publication(self):
        with TemporaryDirectory() as temp:
            raw = generate_world(export_root=temp, keep_geometry_source=True, total_timeout_seconds=180)
            directory = raw.parent
            self.assertEqual(directory.parent, Path(temp))
            self.assertEqual(read_json(directory / "complete.json")["status"], "complete")
            world, coast = read_json(directory / "world.json"), read_json(directory / "coastlines.json")
            validate_outputs(world, coast)
            self.assertEqual(str(read_json(directory / "geometry_source.json")["seed"]), str(world["metadata"]["seed"]))
            self.assertTrue(read_json(raw)["pack"]["burgs"])
            self.assertFalse(list(Path(temp).glob(".staging-*")))


if __name__ == "__main__":
    unittest.main()
