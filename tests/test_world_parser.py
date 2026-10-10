"""Offline regression tests using a real, unfiltered Azgaar world."""

import copy
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shapely.geometry import LineString, Point, box, shape
from shapely.ops import nearest_points, unary_union

import world_parser as wp


FIXTURE = Path(__file__).parent / "fixtures" / "frepoyland.json.gz"


class RealWorldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(FIXTURE, "rt", encoding="utf-8") as stream:
            fixture = json.load(stream)
        cls.data, cls.source = fixture["minimal"], fixture["geometry_source"]
        cls.world = wp.parse_world(cls.data)
        cls.coastlines = wp.build_coastlines(cls.data, cls.source, cls.world, 3)
        cls.geometries = {e["feature_id"]: shape(e["geometry"]) for e in cls.coastlines["landmasses"] + cls.coastlines["water_bodies"]}

    def test_preserves_requested_fields_and_ids(self):
        for name, fields in wp.FIELDS.items():
            original = wp.records(self.data["pack"].get(name))
            actual = self.world["pack"][name]
            self.assertEqual([v["i"] for v in actual], [v["i"] for v in original])
            self.assertEqual(actual, [{k: v[k] for k in fields.split() if k in v} for v in original])
        self.assertEqual(len(self.world["pack"]["burgs"]), 529)
        self.assertEqual(len(self.world["pack"]["states"]), 21)
        self.assertNotIn("nameBases", self.world)
        self.assertNotIn("markets", self.world["pack"])
        self.assertNotIn("vertices", self.world["pack"]["features"][0])

    def test_no_dangling_settlement_states(self):
        states = {s["i"] for s in self.world["pack"]["states"]}
        self.assertTrue(all(b["state"] in states for b in self.world["pack"]["burgs"]))
        bad = copy.deepcopy(self.world)
        bad["pack"]["burgs"][0]["state"] = -100
        with self.assertRaisesRegex(ValueError, "missing state"):
            wp.validate_references(bad)

    def test_substantial_simplification_and_valid_coverage(self):
        stats = self.coastlines["simplification"]
        self.assertGreater(stats["reduction_percent"], 50)
        self.assertLess(stats["simplified_vertices"], stats["original_vertices"])
        wp.validate_outputs(self.world, self.coastlines)
        bounds = box(0, 0, self.coastlines["map_width"], self.coastlines["map_height"])
        union = unary_union(list(self.geometries.values()))
        self.assertLess(bounds.symmetric_difference(union).area, 1e-6)
        self.assertLess(sum(g.area for g in self.geometries.values()) - union.area, 1e-6)

    def test_populated_coastal_islands_retained(self):
        features = {f["i"]: f for f in self.world["pack"]["features"]}
        ports = [b for b in self.world["pack"]["burgs"] if b.get("port") and b.get("population", 0) > 0
                 and features[b["feature"]].get("subtype") in ("island", "isle")]
        self.assertTrue(ports, "Fixture must exercise populated coastal islands")
        for burg in ports:
            self.assertTrue(self.geometries[burg["feature"]].covers(Point(burg["x"], burg["y"])))

    def test_no_landmass_or_lake_disappears(self):
        originals, _ = wp.reconstruct_features(self.data, self.source)
        self.assertEqual(set(originals), set(self.geometries))
        for fid, original in originals.items():
            self.assertEqual(wp.topology_signature(original), wp.topology_signature(self.geometries[fid]))
            self.assertLessEqual(abs(original.area - self.geometries[fid].area), original.area * .1 + 1e-8)
        lakes = [f for f in self.world["pack"]["features"] if f.get("type") == "lake"]
        self.assertTrue(lakes)
        land = unary_union([shape(f["geometry"]) for f in self.coastlines["landmasses"]])
        for lake in lakes:
            point = self.geometries[lake["i"]].representative_point()
            self.assertFalse(land.covers(point))

    def test_point_and_coast_crossing_checks(self):
        burg = next(b for b in self.world["pack"]["burgs"] if b.get("port") and b.get("population", 0) > 0)
        start = Point(burg["x"], burg["y"])
        land = self.geometries[burg["feature"]]
        water = self.geometries[burg["port"]]
        self.assertTrue(land.covers(start))
        end = water.representative_point()
        movement = LineString([start, end])
        crossing = movement.intersection(land.boundary)
        self.assertFalse(crossing.is_empty)
        self.assertTrue(water.covers(end))
        self.assertGreater(movement.project(nearest_points(start, crossing)[1]), 0)

    def test_route_coordinates_and_connectivity_unchanged(self):
        originals = {r["i"]: r for r in wp.records(self.data["pack"]["routes"])}
        for route in self.world["pack"]["routes"]:
            self.assertEqual(route.get("points"), originals[route["i"]].get("points"))
            self.assertEqual(route.get("cells"), originals[route["i"]].get("cells"))

    def test_outputs_reload_without_azgaar_and_raw_is_unchanged(self):
        with TemporaryDirectory() as directory:
            minimal = Path(directory) / "azgaar_minimal.json"
            wp.write_json(minimal, self.data)
            before = minimal.read_bytes()
            world_path, coast_path = wp.process_world(minimal, self.source)
            self.assertEqual(before, minimal.read_bytes())
            wp.validate_outputs(wp.read_json(world_path), wp.read_json(coast_path))
            self.assertLess(world_path.stat().st_size, minimal.stat().st_size / 4)
            with self.assertRaises(FileExistsError):
                wp.process_world(minimal, self.source)

    def test_missing_geometry_and_wrong_seed_are_errors(self):
        for source in ({}, {**self.source, "features": []}, {**self.source, "seed": "wrong"}):
            with self.assertRaises(ValueError):
                wp.build_coastlines(self.data, source, self.world)

    def test_failed_processing_retains_raw_without_partial_outputs(self):
        with TemporaryDirectory() as directory:
            minimal = Path(directory) / "azgaar_minimal.json"
            wp.write_json(minimal, self.data)
            before = minimal.read_bytes()
            with self.assertRaises(ValueError):
                wp.process_world(minimal, {**self.source, "features": []})
            self.assertEqual(minimal.read_bytes(), before)
            self.assertFalse(minimal.with_name("world.json").exists())
            self.assertFalse(minimal.with_name("coastlines.json").exists())

    def test_unclosed_output_geometry_is_rejected(self):
        broken = copy.deepcopy(self.coastlines)
        geo = broken["landmasses"][0]["geometry"]
        polygon = geo["coordinates"] if geo["type"] == "Polygon" else geo["coordinates"][0]
        # mapping() returns tuples; replace the ring with a truncated sequence.
        geo["coordinates"] = [list(polygon[0][:-1])]
        geo["type"] = "Polygon"
        with self.assertRaisesRegex(ValueError, "unclosed"):
            wp.validate_outputs(self.world, broken)

    def test_aggressive_tolerance_still_preserves_settlements_and_features(self):
        coast = wp.build_coastlines(self.data, self.source, self.world, 50)
        wp.validate_outputs(self.world, coast)
        self.assertEqual(len(coast["landmasses"]), len(self.coastlines["landmasses"]))
        self.assertEqual(len(coast["water_bodies"]), len(self.coastlines["water_bodies"]))

    def test_missing_optional_fields_and_real_zero_records(self):
        data = {"info": {}, "pack": {"burgs": [0, {"i": 17, "x": 1, "y": 2, "state": 0}], "states": [0, {"i": 0, "name": "Neutral"}]}}
        world = wp.parse_world(data)
        self.assertEqual(world["pack"]["burgs"][0]["i"], 17)
        self.assertEqual(world["pack"]["states"][0]["i"], 0)
        self.assertNotIn("culture", world["pack"]["burgs"][0])
        self.assertEqual(world["pack"]["religions"], [])

    def test_unicode_surrogates_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unicode.json"
            value = {"name": "Name\ud83d"}
            wp.write_json(path, value)
            self.assertEqual(wp.read_json(path), value)

    def test_zero_tolerance_preserves_exact_geography(self):
        originals, _ = wp.reconstruct_features(self.data, self.source)
        coast = wp.build_coastlines(self.data, self.source, self.world, 0)
        for entry in coast["landmasses"] + coast["water_bodies"]:
            self.assertLess(originals[entry["feature_id"]].symmetric_difference(shape(entry["geometry"])).area, 1e-7)


class GeometryEdgeCases(unittest.TestCase):
    def test_actual_lorteland_map_edge_water_has_no_invented_feature_id(self):
        with gzip.open(FIXTURE.with_name("lorteland.json.gz"), "rt", encoding="utf-8") as stream:
            fixture = json.load(stream)
        data, source = fixture["minimal"], fixture["geometry_source"]
        self.assertEqual(str(data["info"]["seed"]), "312906270")
        world = wp.parse_world(data)
        coast = wp.build_coastlines(data, source, world)
        wp.validate_outputs(world, coast)
        unassigned = [w for w in coast["water_bodies"] if w["feature_id"] is None]
        self.assertEqual(len(unassigned), 1)
        patch = shape(unassigned[0]["geometry"])
        self.assertAlmostEqual(patch.area, 1.9285801630941093, places=7)
        self.assertGreater(patch.intersection(box(0, 0, 1440, 900).boundary).length, 0)
        self.assertEqual({e["feature_id"] for e in coast["landmasses"] + coast["water_bodies"]} - {None},
                         {f["i"] for f in wp.records(data["pack"]["features"])})

    def test_nested_lake_and_populated_island_survive_aggressive_rdp(self):
        outer, lake, island = box(0, 0, 100, 100), box(20, 20, 80, 80), box(49, 49, 51, 51)
        originals = {1: outer.difference(lake), 2: lake.difference(island), 3: island}
        world = {"pack": {"features": [{"i": 1, "land": True}, {"i": 2, "land": False}, {"i": 3, "land": True}],
                          "burgs": [{"i": 99, "feature": 3, "population": 1, "x": 50, "y": 50}]}}
        result, _ = wp.simplify_coverage(originals, outer, world, 100)
        self.assertEqual(set(result), {1, 2, 3})
        self.assertTrue(result[3].covers(Point(50, 50)))
        self.assertEqual(len(result[2].interiors), 1)
        self.assertEqual(len(result[1].interiors), 1)
        wp.validate_coverage(result, outer)

    def test_invalid_coordinates_are_rejected(self):
        self.assertFalse(wp.check_point([float("nan"), 1]))
        self.assertFalse(wp.check_point([True, 1]))
        self.assertFalse(wp.check_point([1, 2, 3]))


if __name__ == "__main__":
    unittest.main()
