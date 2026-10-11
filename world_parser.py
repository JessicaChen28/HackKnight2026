"""Filter Minimal JSON and build a validated, shared RDP coastline coverage.

This module does not import Playwright. Offline use requires a geometry source
snapshot from the SAME world: Minimal JSON alone does not contain coordinates.
"""

import argparse
from collections import defaultdict
import math
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.ops import linemerge, polygonize, unary_union
from shapely.strtree import STRtree
from safe_storage import DEFAULT_MAX_BYTES, checked_path, read_json, write_json, validate_json_tree, safe_message

MAX_ENTITIES = 100_000
MAX_CELLS = 100_000
MAX_VERTICES = 1_000_000


FIELDS = {
    "burgs": "i name x y state culture feature capital port population type group walls citadel temple",
    "states": "i name fullName form formName capital culture neighbors diplomacy campaigns",
    "provinces": "i name fullName state burg",
    "cultures": "i name type origins",
    "religions": "i name type form deity culture origins",
    "markers": "i name type x y note",
    "features": "i name type subtype land",
    "routes": "i name group feature points cells",
    "rivers": "i name type length",
    "zones": "i name type",
}


def records(values):
    """Numeric/null placeholders are not entities; real ID zero records are."""
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_ENTITIES:
        raise ValueError("Entity collection must be an array within the record limit")
    result = []
    for index, value in enumerate(values):
        if value is None or (index == 0 and type(value) is int and value == 0):
            continue
        if not isinstance(value, dict) or "i" not in value:
            raise ValueError("Entity record must contain its original ID")
        if not value.get("removed"):
            result.append(value)
    return result


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def check_point(value):
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(finite_number(v) for v in value)


def validate_metadata(info):
    if not isinstance(info, dict):
        raise ValueError("Required world metadata must be an object")
    for key, limit in (("mapName", 512), ("version", 64)):
        if not isinstance(info.get(key), str) or not info[key].strip() or len(info[key]) > limit:
            raise ValueError(f"Missing or invalid world metadata: {key}")
    seed = info.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, (str, int)) or not str(seed) or len(str(seed)) > 128:
        raise ValueError("Missing or invalid map seed")
    for key in ("width", "height"):
        if not finite_number(info.get(key)) or not 0 < info[key] <= 32768:
            raise ValueError(f"Invalid map {key}; expected a positive number at most 32768")


def validate_minimal(data):
    validate_json_tree(data)
    if not isinstance(data, dict):
        raise ValueError("Minimal export must be a JSON object")
    info, pack = data.get("info"), data.get("pack")
    validate_metadata(info)
    if not isinstance(info.get("description"), str) or "azgaar" not in info["description"].lower():
        raise ValueError("Export metadata does not identify Azgaar")
    if not isinstance(pack, dict):
        raise ValueError("Required pack must be an object")
    if "grid" in data or "cells" in pack or "vertices" in pack:
        raise ValueError("Expected Minimal JSON, not Full or cell data")
    if not isinstance(data.get("settings", {}), dict):
        raise ValueError("Settings must be an object when present")
    for key in FIELDS:
        if key in pack and not isinstance(pack[key], list):
            raise ValueError(f"pack.{key} must be an array")
        entries = records(pack.get(key))
        if key in ("burgs", "states", "features") and not entries:
            raise ValueError(f"Required pack.{key} has no usable records")
        ids = [entry["i"] for entry in entries]
        if any(type(i) is not int or i < 0 for i in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"Invalid or duplicate IDs in {key}")


def parse_world(data: dict) -> dict:
    """Keep only present fields, preserving IDs and index-addressed diplomacy."""
    validate_minimal(data)
    info, settings, pack = data.get("info", {}), data.get("settings", {}), data.get("pack", {})
    metadata = {key: info[key] for key in ("mapName", "version", "seed", "width", "height") if key in info}
    metadata.update({key: settings[key] for key in ("distanceUnit", "distanceScale") if key in settings})
    world = {"schema_version": 1, "metadata": metadata, "pack": {}}
    for collection, fields in FIELDS.items():
        world["pack"][collection] = [
            {key: record[key] for key in fields.split() if key in record}
            for record in records(pack.get(collection))
        ]
    # Current routes contain ordered [x, y, cellId] tuples. Retain them exactly:
    # intermediate cell IDs can be junctions even when the polyline is straight.
    for route in world["pack"]["routes"]:
        if "points" in route:
            if not isinstance(route["points"], list) or any(
                not isinstance(p, list) or len(p) not in (2, 3) or not check_point(p[:2])
                or (len(p) == 3 and (not isinstance(p[2], int) or isinstance(p[2], bool)))
                for p in route["points"]
            ):
                raise ValueError(f"Unsupported coordinate format for route {route['i']}")
        elif route.get("cells"):
            raise ValueError(f"Route {route['i']} has cell IDs but no coordinates; cannot infer its path")
    validate_references(world)
    return world


def validate_references(world):
    if not isinstance(world, dict) or not isinstance(world.get("pack"), dict):
        raise ValueError("World and pack must be objects")
    validate_metadata(world.get("metadata"))
    pack = world["pack"]
    if not pack["burgs"] or not pack["states"]:
        raise ValueError("No settlements or states were extracted")
    for name, values in pack.items():
        if not isinstance(values, list) or any(not isinstance(r, dict) or "i" not in r for r in values):
            raise ValueError(f"Invalid entity records in {name}")
        ids = [r["i"] for r in values]
        if any(type(i) is not int or i < 0 for i in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"Invalid or duplicate IDs in {name}")
        for record in values:
            for key in ("name", "fullName", "type", "group", "formName", "note"):
                if key in record and not isinstance(record[key], str):
                    raise ValueError(f"{name}.{key} must be text")
    states = {s["i"] for s in pack["states"]}
    cultures = {c["i"] for c in pack.get("cultures", [])}
    features = {f["i"]: f for f in pack.get("features", [])}
    if not features:
        raise ValueError("Required geographic features are missing")
    for feature in features.values():
        if (type(feature.get("land")) is not bool or feature.get("type") not in ("island", "lake", "ocean")
                or feature["land"] != (feature["type"] == "island")):
            raise ValueError("Feature must have a land flag and recognized geographic type")
    width, height = world["metadata"]["width"], world["metadata"]["height"]
    for key in ("distanceScale",):
        if key in world["metadata"] and (not finite_number(world["metadata"][key]) or world["metadata"][key] <= 0):
            raise ValueError("Distance scale must be positive and finite")
    if "distanceUnit" in world["metadata"] and not isinstance(world["metadata"]["distanceUnit"], str):
        raise ValueError("Distance unit must be text")
    for burg in pack["burgs"]:
        if type(burg.get("state")) is not int or burg["state"] not in states:
            raise ValueError(f"Settlement {burg['i']} references missing state")
        if not check_point([burg.get("x"), burg.get("y")]):
            raise ValueError(f"Settlement {burg['i']} has no valid coordinates")
        if not (0 <= burg["x"] <= width and 0 <= burg["y"] <= height):
            raise ValueError(f"Settlement {burg['i']} lies outside map bounds")
        if type(burg.get("feature")) is not int or burg["feature"] not in features or not features[burg["feature"]]["land"]:
            raise ValueError(f"Settlement {burg['i']} references missing/non-land feature")
        if "population" in burg and (not finite_number(burg["population"]) or burg["population"] < 0):
            raise ValueError(f"Settlement {burg['i']} has invalid population")
    for collection in ("burgs", "states", "religions"):
        for record in pack.get(collection, []):
            if "culture" in record and (type(record["culture"]) is not int or record["culture"] not in cultures):
                raise ValueError(f"{collection} {record['i']} references missing culture")
    burgs = {b["i"] for b in pack["burgs"]}
    for state in pack["states"]:
        if "capital" in state and (type(state["capital"]) is not int or state["capital"] not in burgs | {0}):
            raise ValueError(f"State {state['i']} references missing capital")
    for route in pack.get("routes", []):
        if "feature" in route and (type(route["feature"]) is not int or route["feature"] not in features):
            raise ValueError("Route references missing geographic feature")
        for point in route.get("points", []):
            if not (0 <= point[0] <= width and 0 <= point[1] <= height):
                raise ValueError("Route point lies outside map bounds")
    for marker in pack.get("markers", []):
        if "x" in marker or "y" in marker:
            if not check_point([marker.get("x"), marker.get("y")]) or not (0 <= marker["x"] <= width and 0 <= marker["y"] <= height):
                raise ValueError("Marker has invalid or out-of-bounds coordinates")


def polygon_parts(geometry):
    return list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]


def topology_signature(geometry):
    return sorted(len(p.interiors) for p in polygon_parts(geometry))


def vertex_count(geometry):
    return sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in polygon_parts(geometry))


def require_polygon(geometry, label):
    if not isinstance(geometry, (Polygon, MultiPolygon)) or geometry.is_empty or not geometry.is_valid:
        raise ValueError(f"{label}: invalid, empty, or non-polygon geometry")


def reconstruct_features(data, source):
    """Union actual ordered cell rings; clip only to the exported map rectangle."""
    if not isinstance(source, dict) or not isinstance(source.get("features"), list):
        raise ValueError("Geometry source must contain a features array")
    validate_json_tree(source)
    info = data["info"]
    if str(source.get("seed")) != str(info.get("seed")):
        raise ValueError("Geometry source seed does not match Minimal JSON")
    width, height = info.get("width"), info.get("height")
    if not all(finite_number(n) and n > 0 for n in (width, height)):
        raise ValueError("Missing or invalid map dimensions")
    if (source.get("map_width"), source.get("map_height")) != (width, height):
        raise ValueError("Geometry source dimensions do not match Minimal JSON")
    if source.get("coordinate_system") != "azgaar_map":
        raise ValueError("Unsupported geometry coordinate system")
    features = {f["i"]: f for f in records(data["pack"].get("features"))}
    expected = set(features)
    bounds, originals = box(0, 0, width, height), {}
    total_cells = total_vertices = 0
    for feature in source.get("features", []):
        if not isinstance(feature, dict) or type(feature.get("feature_id")) is not int or not isinstance(feature.get("cells"), list):
            raise ValueError("Invalid geometry source feature structure")
        fid = feature["feature_id"]
        if fid in originals or fid not in expected:
            raise ValueError(f"Duplicate or unknown geometry feature {fid}")
        if "cells" in features[fid] and len(feature.get("cells", [])) != features[fid]["cells"]:
            raise ValueError(f"Feature {fid} cell count does not match Minimal JSON")
        cells = []
        total_cells += len(feature["cells"])
        if total_cells > MAX_CELLS:
            raise ValueError("Geometry exceeds the cell limit")
        for ring in feature.get("cells", []):
            if not isinstance(ring, list) or len(ring) < 3 or not all(check_point(p) for p in ring):
                raise ValueError(f"Feature {fid} has invalid cell coordinates")
            total_vertices += len(ring)
            if total_vertices > MAX_VERTICES:
                raise ValueError("Geometry exceeds the vertex limit")
            # Voronoi boundary vertices legitimately extend beyond the map.
            # Bound their magnitude before GEOS; clip them as before afterward.
            if any(abs(p[0]) > width * 16 or abs(p[1]) > height * 16 for p in ring):
                raise ValueError("Source vertex exceeds the supported Voronoi envelope")
            polygon = Polygon(ring)
            require_polygon(polygon, f"Feature {fid} source cell")
            cells.append(polygon)
        if not cells:
            raise ValueError(f"Feature {fid} has no coordinate geometry")
        geometry = unary_union(cells).intersection(bounds)
        require_polygon(geometry, f"Feature {fid}")
        originals[fid] = geometry
    if not originals or set(originals) != expected:
        raise ValueError("Geometry extraction is incomplete: missing land/water features")
    # Pack.generate drops deep ocean sites. Boundary pseudo-sites have no cells,
    # so union(pack.cells) does NOT cover the viewport. The rendered sea is the
    # map rectangle minus land and lakes. Recover each connected sea component
    # this way and identify it by its actual overlapping ocean cells. Never guess
    # an ID if the source does not establish a unique ocean owner.
    oceans = {fid: g for fid, g in originals.items() if features[fid].get("type") == "ocean"}
    occupied = unary_union([g for fid, g in originals.items() if fid not in oceans])
    sea = bounds.difference(occupied)
    ocean_parts = defaultdict(list)
    unassigned_water = []
    mesh = unary_union(list(originals.values()))
    for component in ([] if sea.is_empty else polygon_parts(sea)):
        owners = [fid for fid, g in oceans.items() if component.intersection(g).area > 1e-8]
        if not owners and component.intersection(bounds.boundary).length > 0:
            # Boundary pseudo-sites can leave a disconnected background-water
            # patch with NO real ocean cells (e.g. seed 312906270). Preserve its
            # actual boundary, but do not invent or borrow an Azgaar feature ID.
            if component.intersection(mesh).area <= max(bounds.area * 1e-10, 1e-8):
                unassigned_water.append(component)
                continue
        if len(owners) != 1:
            raise ValueError(f"Cannot assign ocean geometry unambiguously to source features: {owners}; area={component.area}, bounds={component.bounds}")
        ocean_parts[owners[0]].append(component)
    if set(ocean_parts) != set(oceans):
        raise ValueError("An ocean feature was lost while reconstructing the map coverage")
    originals.update({fid: unary_union(parts) for fid, parts in ocean_parts.items()})
    if unassigned_water:
        originals[None] = unary_union(unassigned_water)
    validate_coverage(originals, bounds)
    return originals, bounds


def validate_coverage(geometries, bounds):
    values = list(geometries.values())
    for fid, geometry in geometries.items():
        require_polygon(geometry, f"Feature {fid}")
    united = unary_union(values)
    # Area-only floating-point slack; settlement containment is checked separately.
    slack = max(bounds.area * 1e-10, 1e-8)
    if bounds.symmetric_difference(united).area > slack:
        raise ValueError("Land/water polygons leave gaps or extend beyond map bounds")
    if sum(g.area for g in values) - united.area > slack:
        raise ValueError("Land/water feature interiors overlap")


def settlement_failures(world, geometries):
    land = {f["i"] for f in world["pack"]["features"] if f.get("land")}
    failed = []
    for burg in world["pack"]["burgs"]:
        if burg.get("population", 0) <= 0:
            continue
        fid = burg.get("feature")
        if fid not in land or fid not in geometries:
            raise ValueError(f"Populated settlement {burg['i']} has no land feature")
        if not geometries[fid].covers(Point(burg["x"], burg["y"])):
            failed.append(burg)
    return failed


def protect_coastal_arcs(arcs, world, tolerance):
    """Pin original shore segments near populated towns before applying RDP.

    A single port should not force the entire continent to use tiny tolerances.
    These split points preserve the local coast; all other arcs can simplify.
    """
    points = [Point(b["x"], b["y"]) for b in world["pack"]["burgs"] if b.get("population", 0) > 0]
    if not points or tolerance == 0:
        return arcs
    tree = STRtree(points)
    output = []
    for arc in arcs:
        coords = list(arc.coords)
        cuts = {0, len(coords) - 1}
        for i in range(len(coords) - 1):
            segment = LineString(coords[i:i + 2])
            if any(segment.distance(points[int(j)]) <= tolerance
                   for j in tree.query(segment, predicate="dwithin", distance=tolerance)):
                cuts.update((i, i + 1))
        ordered = sorted(cuts)
        output.extend(LineString(coords[a:b + 1]) for a, b in zip(ordered, ordered[1:]))
    return output


def simplify_coverage(originals, bounds, world, tolerance):
    """RDP on shared arcs, polygonized once; back off only affected boundaries.

    Independent Polygon.simplify calls can disagree at shared shores. Deduplicate
    and merge the original boundary network first so BOTH sides use each arc.
    """
    if not finite_number(tolerance) or tolerance < 0:
        raise ValueError("Coastline tolerance must be finite and nonnegative")
    failed = settlement_failures(world, originals)
    if failed:
        raise ValueError(f"Source mesh already places populated settlements off land: {[b['i'] for b in failed]}")
    network = unary_union([g.boundary for g in originals.values()])
    merged = linemerge(network) if network.geom_type == "MultiLineString" else network
    arcs = list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]
    arcs = protect_coastal_arcs(arcs, world, tolerance)
    ids, original_values = list(originals), list(originals.values())
    tree = STRtree(original_values)
    incidence = []
    for arc in arcs:
        incidence.append({ids[int(i)] for i in tree.query(arc) if original_values[int(i)].boundary.intersection(arc).length > 0})
    # Interior witnesses bind every connected component to its original ID.
    witnesses, owners = [], []
    for fid, geometry in originals.items():
        for polygon in polygon_parts(geometry):
            witnesses.append(polygon.representative_point())
            owners.append(fid)
    witness_tree = STRtree(witnesses)
    tolerances = [float(tolerance)] * len(arcs)
    # The map edge is a clipping boundary, not a coast, and must not move.
    for i, arc in enumerate(arcs):
        if arc.intersection(bounds.boundary).length > 0:
            tolerances[i] = 0
    result = None
    for attempt in range(11):
        candidates = []
        for arc, amount in zip(arcs, tolerances):
            if amount == 0:
                candidates.append(arc)
            elif arc.is_ring:
                # Topology-preserving closed-ring RDP never collapses tiny islands.
                candidates.append(LineString(Polygon(arc.coords).simplify(amount, preserve_topology=True).exterior.coords))
            else:
                candidates.append(arc.simplify(amount, preserve_topology=True))
        bad, faces = set(), defaultdict(list)
        for face in polygonize(unary_union(candidates)):
            contained = {owners[int(i)] for i in witness_tree.query(face) if face.covers(witnesses[int(i)])}
            if len(contained) != 1:
                bad.update(contained)
                bad.update(ids[int(i)] for i in tree.query(face) if original_values[int(i)].intersection(face).area > 0)
                continue
            faces[contained.pop()].append(face)
        result = {fid: unary_union(parts) for fid, parts in faces.items()}
        for fid, original in originals.items():
            candidate = result.get(fid)
            if candidate is None or candidate.is_empty or not candidate.is_valid:
                bad.add(fid)
            elif not isinstance(candidate, (Polygon, MultiPolygon)):
                bad.add(fid)
            elif topology_signature(candidate) != topology_signature(original):
                bad.add(fid)
            elif abs(candidate.area - original.area) > original.area * 0.1:
                bad.add(fid)
        if not bad:
            bad.update(b["feature"] for b in settlement_failures(world, result))
        if not bad:
            try:
                validate_coverage(result, bounds)
            except ValueError:
                bad.update(originals)
        if not bad:
            break
        # Keep reducing only arcs bordering affected features; on the final
        # attempts restore their exact source boundaries. No geometry repair or
        # settlement relocation is used to hide an inconsistent source.
        for i, neighbors in enumerate(incidence):
            if neighbors & bad:
                tolerances[i] = tolerances[i] / 2 if attempt < 7 else 0
    else:
        result = originals
        tolerances = [0.0] * len(arcs)
    return result, {
        "algorithm": "RDP shared boundary arcs (Shapely/GEOS)",
        "requested_tolerance": tolerance,
        "minimum_used_tolerance": min(tolerances),
        "maximum_used_tolerance": max(tolerances),
        "arcs": len(arcs),
        "arcs_below_requested_tolerance": sum(t < tolerance for t in tolerances),
    }


def build_coastlines(data, source, world, tolerance=3.0):
    originals, bounds = reconstruct_features(data, source)
    simplified, stats = simplify_coverage(originals, bounds, world, tolerance)
    original_count = sum(vertex_count(g) for g in originals.values())
    simplified_count = sum(vertex_count(g) for g in simplified.values())
    stats.update(original_vertices=original_count, simplified_vertices=simplified_count,
                 reduction_percent=round(100 * (1 - simplified_count / original_count), 2))
    output = {
        "schema_version": 1, "map_width": data["info"]["width"], "map_height": data["info"]["height"],
        "seed": data["info"]["seed"], "coordinate_system": "azgaar_map",
        "geometry_basis": "geographic cell mesh; excludes decorative fractal displacement",
        "simplification": stats, "landmasses": [], "water_bodies": [],
    }
    for feature in world["pack"]["features"]:
        fid = feature["i"]
        entry = {"feature_id": fid, **{k: feature[k] for k in ("name", "type", "subtype") if k in feature},
                 "geometry": mapping(simplified[fid])}
        output["landmasses" if feature.get("land") else "water_bodies"].append(entry)
    if None in simplified:
        output["water_bodies"].append({
            "feature_id": None, "type": "ocean",
            "source": "map-edge background water without an Azgaar feature ID",
            "geometry": mapping(simplified[None]),
        })
    validate_outputs(world, output)
    return output


def validate_outputs(world, coastlines):
    """Validate reloaded products without requiring browser state or the source."""
    validate_json_tree(world)
    validate_json_tree(coastlines)
    validate_references(world)
    metadata = world["metadata"]
    if str(metadata.get("seed")) != str(coastlines.get("seed")):
        raise ValueError("World/coastline seeds differ")
    width, height = coastlines["map_width"], coastlines["map_height"]
    if (metadata.get("width"), metadata.get("height")) != (width, height):
        raise ValueError("World/coastline dimensions differ")
    if not all(finite_number(n) and n > 0 for n in (width, height)):
        raise ValueError("Invalid coastline dimensions")
    if coastlines.get("coordinate_system") != "azgaar_map":
        raise ValueError("Unknown coastline coordinate system")
    geometries = {}
    for entry in coastlines["landmasses"] + coastlines["water_bodies"]:
        fid, geojson = entry["feature_id"], entry["geometry"]
        if fid in geometries:
            raise ValueError(f"Duplicate geometry feature {fid}")
        if geojson.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"Feature {fid} has an unsupported geometry type")
        polygons = [geojson["coordinates"]] if geojson["type"] == "Polygon" else geojson["coordinates"]
        if not polygons or any(not polygon for polygon in polygons):
            raise ValueError(f"Feature {fid} has empty rings")
        for polygon in polygons:
            for ring in polygon:
                if len(ring) < 4 or ring[0] != ring[-1] or not all(check_point(p) for p in ring):
                    raise ValueError(f"Feature {fid} has invalid or unclosed coordinates")
                if any(not (-1e-7 <= x <= width + 1e-7 and -1e-7 <= y <= height + 1e-7) for x, y in ring):
                    raise ValueError(f"Feature {fid} has coordinates outside map bounds")
        geometries[fid] = shape(geojson)
    expected = {f["i"] for f in world["pack"]["features"]}
    expected_land = {f["i"] for f in world["pack"]["features"] if f.get("land")}
    if set(geometries) - {None} != expected or {f["feature_id"] for f in coastlines["landmasses"]} != expected_land:
        raise ValueError("Missing, added, or misclassified landmasses/water bodies")
    if None in geometries:
        entry = next(e for e in coastlines["water_bodies"] if e["feature_id"] is None)
        bounds = box(0, 0, width, height)
        if entry.get("type") != "ocean" or any(
            p.intersection(bounds.boundary).length == 0 for p in polygon_parts(geometries[None])
        ):
            raise ValueError("Unassigned water must be ocean background touching the map edge")
    validate_coverage(geometries, box(0, 0, width, height))
    failed = settlement_failures(world, geometries)
    if failed:
        raise ValueError(f"Populated settlements are off land: {[b['i'] for b in failed]}")


def process_world(minimal_path, source, tolerance=3.0, max_input_bytes=DEFAULT_MAX_BYTES):
    """Write products beside a retained Minimal file, refusing existing products."""
    minimal_path = checked_path(minimal_path)
    world_path, coast_path = minimal_path.parent / "world.json", minimal_path.parent / "coastlines.json"
    if world_path.exists() or coast_path.exists():
        raise FileExistsError("Processed outputs already exist; use a new directory to reprocess")
    data = read_json(minimal_path, max_input_bytes)
    world = parse_world(data)
    coastlines = build_coastlines(data, source, world, tolerance)
    written = []
    pending = minimal_path.parent / ".incomplete.json"
    complete = minimal_path.parent / "complete.json"
    if complete.exists():
        raise FileExistsError("Completed world already exists")
    write_json(pending, {"status": "processing"})
    try:
        for path, value in ((world_path, world), (coast_path, coastlines)):
            write_json(path, value)
            written.append(path)
        validate_outputs(read_json(world_path, max_input_bytes), read_json(coast_path, max_input_bytes))
        write_json(complete, {"schema_version": 1, "status": "complete", "files": [minimal_path.name, world_path.name, coast_path.name]})
        written.append(complete)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    finally:
        pending.unlink(missing_ok=True)
    stats, pack = coastlines["simplification"], world["pack"]
    print(f"World: {safe_message(world['metadata'].get('mapName', '(unnamed)'))}")
    print(f"Settlements: {len(pack['burgs'])}\nStates (including neutral): {len(pack['states'])}\nProvinces: {len(pack['provinces'])}")
    print(f"Landmasses: {len(coastlines['landmasses'])}\nWater bodies: {len(coastlines['water_bodies'])}")
    if any(e["feature_id"] is None for e in coastlines["water_bodies"]):
        print("Note: map-edge background water retained with feature_id=null (Azgaar supplies no ID).")
    print(f"Original coastline vertices: {stats['original_vertices']}\nSimplified coastline vertices: {stats['simplified_vertices']}\nReduction: {stats['reduction_percent']}%")
    print(f"Original Minimal JSON: {minimal_path.stat().st_size / 1_000_000:.2f} MB\nFiltered world JSON: {world_path.stat().st_size / 1000:.1f} KB\nCoastline JSON: {coast_path.stat().st_size / 1000:.1f} KB")
    print("Validation: PASSED", flush=True)
    return world_path, coast_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("minimal", type=Path)
    parser.add_argument("--geometry-source", type=Path, required=True, help="Same-world snapshot saved with --keep-geometry-source")
    parser.add_argument("--coastline-tolerance", type=float, default=3.0)
    parser.add_argument("--max-input-mb", type=int, default=64)
    args = parser.parse_args()
    try:
        limit = args.max_input_mb * 1024 * 1024
        process_world(args.minimal, read_json(args.geometry_source, limit), args.coastline_tolerance, limit)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"Processing failed: {safe_message(error)}\n")


if __name__ == "__main__":
    main()
