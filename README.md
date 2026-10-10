# HackKnight 2026

Team: Hasan, Ayesha, and Jessica.

## Azgaar world exporter and geographic parser

A standalone Python program that generates a fresh random world using Azgaar's own procedural pipeline, retains its **original Minimal JSON export**, and builds a compact RPG world with simplified geographic boundaries. Requires Python 3.10+ and internet access for generation. Offline parsing/tests require no browser or network. Dependencies are Playwright and Shapely (which installs NumPy).

## Install

From this project's root directory:

```sh
python -m venv .venv
```

Activate the environment on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on macOS / Linux:

```sh
source .venv/bin/activate
```

Then install the dependency and browser:

```sh
python -m pip install -r requirements.txt
python -m playwright install chromium
```

For a one-command setup, run the helper appropriate to your system from the project root:

```powershell
.\setup.ps1
```

```sh
sh setup.sh
```

The helpers create a project-local `.venv`, install the pinned dependencies, and install Playwright Chromium. PowerShell may require this one-session policy override before running `setup.ps1`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

If you already use Conda, activate that environment first and run the same two commands there. The `python` used for installation must be the same `python` used to run the program. Verify it with `python -c "import sys; print(sys.executable)"`.

On Linux, if browser system libraries are missing, use `python -m playwright install --with-deps chromium`.

## Run

```sh
python generate_world.py
```

On Windows, you can double-click `run_generate_world.bat` or run it from a terminal. It selects this project's `.venv` automatically when present and pauses after errors so the message remains visible. Running a `.py` file by double-clicking can close the console before an environment error can be read.

Show the browser while debugging or allow more time on a slow connection:

```sh
python generate_world.py --headed --timeout 300
```

Control the maximum Ramer–Douglas–Peucker (RDP) simplification tolerance in Azgaar map coordinates:

```sh
python generate_world.py --coastline-tolerance 3
```

Each invocation creates an isolated browser session, so Azgaar generates a new random world. During the **same session**, the script downloads Minimal JSON and reads the geographic mesh. Files are saved under `exports/` beside the script, regardless of working directory. The world directory uses Azgaar's suggested name plus a UTC timestamp and collision counter; atomic directory creation prevents overwriting earlier runs.

```text
exports/
  Orleans Minimal ..._<unique timestamp>/
    azgaar_minimal.json
    world.json
    coastlines.json
```

Existing flat JSON exports are left in place. `generate_world(...)` still returns the `Path` to the original export, now named `azgaar_minimal.json` inside the new directory.

The original download remains byte-for-byte unchanged. Validation failures leave both the original and the captured `geometry_source.json` for diagnosis and report the failure; incomplete processed files are removed. Successful runs keep the source snapshot only with `--keep-geometry-source`. Browser resources close on success, failure, or Ctrl+C. Failures return a nonzero exit code. `--timeout` applies separately to each browser operation, not the entire run.

If Azgaar produces inconsistent geography (for example, a settlement already outside its land feature), generation automatically tries a fresh random world, up to **five attempts**. Use `--attempts 1` to disable replacements or `--attempts N` to choose the limit. Rejected directories include `validation_error.json` and diagnostic inputs; they are not usable processed worlds. The final `Validation: PASSED` and `Saved world files:` messages identify the successful world directory. Disk, dependency, and network errors are not retried as bad worlds. Retrying cannot guarantee success if the upstream schema changes; exhaustion reports a nonzero exit status with the last validation error.

## Output schemas

Both processed files have `schema_version: 1`. Absent optional fields remain absent. Numeric/null placeholders and removed records are excluded; **real ID-zero records**, such as the neutral state, remain. IDs are never renumbered. Diplomacy arrays retain their original positions, which refer to state IDs.

`world.json` contains:

- `metadata`: available `mapName`, `version`, `seed`, `width`, `height`, `distanceUnit`, `distanceScale` from the original export. Scale and population values retain Azgaar's native semantics; neither is converted.
- `pack`: `burgs`, `states`, `provinces`, `cultures`, `religions`, `markers`, `features`, `routes`, `rivers`, and `zones`. The allowlists are defined in `world_parser.FIELDS` and match the requested narrative fields. No goods, markets, heraldry, military units, UI options, name tables, or feature vertex indices are retained.
- Routes retain their original ordered `points` (`[x, y, cellId]` in the inspected version), any `cells`, and `feature`. These are intentionally not simplified: intermediate cell IDs can encode junctions. They are not treated as polygon rings. Unknown route coordinate formats fail explicitly instead of inventing coordinates.

`coastlines.json` contains:

- `map_width`, `map_height`, `seed`, `coordinate_system: "azgaar_map"`, and `geometry_basis`.
- `landmasses` and `water_bodies`: each entry contains `feature_id`, available `name`, `type`, `subtype`, and GeoJSON-style `geometry` of type `Polygon` or `MultiPolygon`. Water includes oceans and lakes. Subtypes such as frozen/dry/lava are preserved as upstream classifies them; no gameplay interpretation is applied.
- Polygon coordinates are ordered closed `[x, y]` rings: exterior first, holes afterward. MultiPolygon coordinates contain a list of such polygons. These are planar map coordinates, **not longitude/latitude**. X increases right/east and Y down/south, with the map rectangle from `(0, 0)` to `(width, height)`.
- `simplification`: requested/used tolerances, arc counts, original/simplified vertex counts, and reduction percentage. Counts include closing coordinates, lake holes, and both sides of shared boundaries, consistently before and after simplification.

## Geometry and correctness

Minimal JSON includes feature vertex **IDs**, but omits `pack.vertices.p`, the coordinate table. `geometry_source.py` reads ordered polygons from `pack.cells.v`, resolves them through `pack.vertices.p`, and groups them by `pack.cells.f`. The parser unions these cells and clips to the map rectangle. This captures multiple components, lakes as holes, and islands within lakes. A single `feature.vertices` outline is insufficient to represent all holes, and ocean features have empty vertex arrays.

Azgaar drops deep-ocean cells and gives its boundary pseudo-sites no polygons. Consequently, the packed mesh does not fill the map. The parser derives sea regions from the map rectangle minus land and lakes, then associates each connected sea region with the unique overlapping original ocean feature. A disconnected map-edge background-water patch may contain no source ocean cells at all. Such patches are preserved together as an ocean Polygon/MultiPolygon with `feature_id: null` and an explicit `source` description, since Azgaar provides no feature ID for them. All original feature IDs remain intact. Interior regions without an owner, or regions overlapping multiple ocean features, still fail validation rather than receiving guessed IDs.

Geometry follows Azgaar's **geographic cell mesh**, which its settlement placement uses. Decorative fractal displacement and SVG curve smoothing are not part of this representation. No source objects or original coordinates are modified in the browser.

RDP runs through Shapely/GEOS on a deduplicated network of shared boundary arcs. Shared shores are simplified once and polygonized into both adjoining regions, avoiding mismatched lake holes, gaps, and overlaps. Original shore segments near populated settlements are protected. Closed rings use topology-preserving polygon RDP; open arcs retain endpoints. Invalid results, lost components/holes, more than 10% feature area change, or populated settlements off their original land feature trigger lower tolerances on affected boundaries, eventually reverting to the original geometry if necessary. Small islands are never intentionally discarded.

Every output is reloaded and checked for valid JSON, finite closed coordinate rings, valid nonempty polygons, complete feature IDs, settlement/state references, populated settlement containment, and complete, nonoverlapping map coverage. The source mesh must already place populated settlements on land; inconsistent source worlds fail with the relevant IDs. Boundary points count as on land (`covers`). Polygon interiors identify land/water unambiguously; a point exactly on a shared shore belongs to both closed polygon boundaries. Coordinates outside the map are outside the modeled world.

Tolerance `3` means three map units, independent of the map's distance display unit. `0` preserves the geographic coverage without RDP displacement. Large tolerances are only an upper bound: protecting coastal towns and small islands can reduce the effective tolerance, so increasing the value does not guarantee smaller files. No navigation/pathfinding or gameplay decisions are implemented.

## Offline use and reprocessing

```sh
python generate_world.py --keep-geometry-source
```

This retains an optional fourth file, `geometry_source.json`, containing the same-world raw cell rings, seed, and dimensions. It is larger than the processed coastline file and is useful for debugging or trying other tolerances without reopening Azgaar. Failed parsing runs retain this snapshot automatically, even without the flag.

To reprocess, copy `azgaar_minimal.json` and `geometry_source.json` into a **new directory** (existing processed files are never overwritten), then run:

```sh
python world_parser.py "exports/reprocess/azgaar_minimal.json" --geometry-source "exports/reprocess/geometry_source.json" --coastline-tolerance 2
```

The original two test exports support narrative parsing with `parse_world(data)`, but cannot supply coastlines because they lack the coordinate table. A snapshot from a different world is rejected. For future geography checks, only the processed files are required:

```python
import json
from pathlib import Path
from shapely.geometry import Point, LineString, shape
from world_parser import validate_outputs

directory = Path("exports/<world directory>")
world = json.loads((directory / "world.json").read_text(encoding="utf-8"))
coasts = json.loads((directory / "coastlines.json").read_text(encoding="utf-8"))
validate_outputs(world, coasts)

burg = world["pack"]["burgs"][0]
entry = next(f for f in coasts["landmasses"] if f["feature_id"] == burg["feature"])
land = shape(entry["geometry"])
start = Point(burg["x"], burg["y"])
assert land.covers(start)
# A geometric query only; choosing movement distance/outcomes is future work.
segment = LineString([(start.x, start.y), (start.x + 20, start.y)])
intersections = segment.intersection(land.boundary)
```

## Tests and measured results

```sh
python -m unittest discover -s tests -v
```

The tests use a compressed **actual Azgaar world** under `tests/fixtures/` and run offline. They cover field/ID preservation, state references, original bytes remaining unchanged, populated coastal islands, lakes, nested holes, point containment, coast crossings, route connectivity, missing/mismatched geometry, Unicode edge cases, and independent reloading. Synthetic nested geometry supplements the real-world tests.

Live captures on October 10, 2026 passed validation. With tolerance 3:

| World | Settlements | Land / water features | Vertices before / after | Reduction |
| --- | ---: | ---: | ---: | ---: |
| Frepoyland (offline fixture) | 529 | 20 / 6 | 4,614 / 1,796 | 61.07% |
| Mamia | 868 | 11 / 8 | 4,235 / 1,411 | 66.68% |
| Orleans (complete default-command run) | 669 | 9 / 3 | 3,317 / 1,323 | 60.11% |

Orleans' original Minimal export was 2.46 MB; its `world.json` was 282.0 KB and `coastlines.json` 52.9 KB (decimal sizes). These are observed results, not guarantees. The 80–95% vertex-reduction target was not reached on these worlds; exact coastal settlement placement and topology take priority.

## Verified integration points

Inspected against the live site and upstream source on October 10, 2026:

- [Startup](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/services/url-params.ts) calls `generateMapOnLoad` in a fresh session with no seed or saved map.
- [Lifecycle](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/components/lifecycle.ts) dispatches `map:generated` after `GenerationPipeline.run` and map registration. The listener is installed before navigation. The loading overlay alone does **not** signal completion.
- The [live interface](https://azgaar.github.io/Fantasy-Map-Generator/) uses `#optionsTrigger`, `#exportButton`, and `#exportMapData button[onclick="exportToJson('Minimal')"]`. The script clicks this exact Minimal control, never Full.
- [JSON export source](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/services/io/export-json.ts) creates an `application/json` Blob and clicks an anchor with a `download` filename. Playwright's `expect_download` captures this normally, even though the Blob URL is immediately revoked. No image scraping, custom serializer, or download fallback is needed.
- [Feature generation](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/generators/features-generator.ts) associates feature vertex IDs with `pack.vertices.p` and leaves ocean outlines empty.
- [Packed graph](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/generators/pack-generator.ts) resolves cell polygons and excludes deep-ocean sites; [Voronoi construction](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/generators/voronoi.ts) gives boundary pseudo-sites no cells.
- [Coastline renderer geometry](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/generators/coastline-generator.ts) adds decorative displacement to the mesh; [burg placement](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/generators/burgs-generator.ts) places ports toward geographic cell edges.
- [Routes](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/src/generators/routes-generator.ts) use `[x, y, cellId]` points and cell IDs for connectivity.

Azgaar is a live upstream application; changes to these selectors, its completion event, or export schema may require updating this small adapter. Use `--headed` to investigate failures.
