"""Generate an Azgaar world and download its original Minimal JSON export."""

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from geometry_source import extract_geometry

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    from world_parser import process_world, write_json
except ModuleNotFoundError as error:
    if error.name in ("playwright", "shapely", "numpy"):
        print(
            f"Required dependency {error.name} is not installed in this Python environment.\n"
            "Activate the environment you want to use, then run:\n"
            "  python -m pip install -r requirements.txt\n"
            "  python -m playwright install chromium",
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    raise


URL = "https://azgaar.github.io/Fantasy-Map-Generator/"
EXPORTS = Path(__file__).resolve().parent / "exports"
MINIMAL_BUTTON = "#exportMapData button[onclick=\"exportToJson('Minimal')\"]"

# Installed before navigation so even a fast initial generation cannot be missed.
# This public event is dispatched by registerMap AFTER GenerationPipeline.run.
GENERATION_LISTENER = """
window.__azgaarGenerated = null;
window.addEventListener('map:generated', event => {
    window.__azgaarGenerated = event.detail;
});
"""


def validate_export(path: Path) -> dict:
    """Check the original file without filtering or reserializing its contents."""
    with path.open(encoding="utf-8-sig") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("Export must be a JSON object")
    info, pack = data.get("info"), data.get("pack")
    if not isinstance(info, dict) or not isinstance(pack, dict):
        raise ValueError("Export is missing Azgaar info or pack data")
    if "azgaar" not in str(info.get("description", "")).lower():
        raise ValueError("Export does not identify itself as Azgaar world data")
    for key in ("burgs", "states"):
        records = pack.get(key)
        if not isinstance(records, list) or not records:
            raise ValueError(f"Expected a nonempty pack.{key} array")
        # Azgaar reserves index 0 (e.g. neutral state / placeholder settlement).
        if not (isinstance(records[0], dict) or records[0] == 0):
            raise ValueError(f"pack.{key} has an invalid index-zero placeholder")
        if not all(isinstance(record, dict) for record in records[1:]):
            raise ValueError(f"pack.{key} contains invalid records")
    if "grid" in data or "cells" in pack or "vertices" in pack:
        raise ValueError("Received full/cell data instead of Minimal JSON")
    return data


def reserve_world_directory(suggested: str, world_name: str) -> Path:
    """Reserve a safe unique world directory, including for concurrent runs."""
    stem = Path(suggested.replace("\\", "/")).stem or world_name or "world"
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:100] or "world"
    # Prefix Windows device names, which are reserved even with an extension.
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", stem, re.I):
        stem = f"world_{stem}"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    counter = 0
    while True:
        suffix = f"_{counter}" if counter else ""
        path = EXPORTS / f"{stem}_{timestamp}{suffix}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            counter += 1


def generate_world(headless: bool = True, timeout_seconds: int = 180,
                   coastline_tolerance: float = 3.0, keep_geometry_source: bool = False) -> Path:
    if not math.isfinite(coastline_tolerance) or coastline_tolerance < 0:
        raise ValueError("Coastline tolerance must be finite and nonnegative")
    EXPORTS.mkdir(parents=True, exist_ok=True)
    timeout_ms = timeout_seconds * 1000
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, timeout=timeout_ms)
        try:
            # No persistent profile, saved map, seed, or cached user preferences.
            context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
            try:
                context.add_init_script(GENERATION_LISTENER)
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
                page.on("pageerror", lambda error: print(f"[browser] {error}", file=sys.stderr))
                print("Opening Azgaar; generating a new random world...", flush=True)
                page.goto(URL, wait_until="domcontentloaded")
                page.wait_for_function("""() =>
                    window.__azgaarGenerated !== null ||
                    document.querySelector('#errorBox') !== null
                """)
                error = page.locator("#errorBox")
                if error.count():
                    raise RuntimeError(f"Azgaar generation failed: {error.inner_text()}")
                # The loading screen alone is insufficient: startup hides it
                # before generation. Require the completion event AND world data.
                page.wait_for_function("""() =>
                    Array.isArray(window.pack?.burgs) && window.pack.burgs.length > 0 &&
                    Array.isArray(window.pack?.states) && window.pack.states.length > 0
                """)
                page.locator("#loading").wait_for(state="hidden")
                generated = page.evaluate("window.__azgaarGenerated")
                print(f"Generation complete (seed {generated['seed']}). Opening Export...", flush=True)

                if not page.locator("#exportButton").is_visible():
                    page.locator("#optionsTrigger").click()
                page.locator("#exportButton").click()
                page.locator("#exportMapData").wait_for(state="visible")
                print("Downloading Azgaar's Minimal JSON export...", flush=True)
                with page.expect_download(timeout=timeout_ms) as pending:
                    page.locator(MINIMAL_BUTTON).click()
                download = pending.value

                # Validate a temporary download before publishing a .json file.
                with NamedTemporaryFile(dir=EXPORTS, prefix=".azgaar-", suffix=".part", delete=False) as temp:
                    temporary = Path(temp.name)
                destination = None
                try:
                    download.save_as(temporary)
                    data = validate_export(temporary)
                    if str(data["info"].get("seed")) != str(generated["seed"]):
                        raise ValueError("Downloaded world seed does not match the generated world")
                    directory = reserve_world_directory(download.suggested_filename, data["info"].get("mapName", "world"))
                    destination = directory / "azgaar_minimal.json"
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)

                print("Extracting geographic cell coordinates from this world...", flush=True)
                try:
                    geometry_source = extract_geometry(page)
                    if keep_geometry_source:
                        write_json(directory / "geometry_source.json", geometry_source)
                    print("Filtering world data and simplifying coastlines...", flush=True)
                    process_world(destination, geometry_source, coastline_tolerance)
                except Exception:
                    print(f"World processing failed. Original Minimal JSON retained at: {destination}", file=sys.stderr)
                    raise
                print(f"Saved world files: {directory}", flush=True)
                # Preserve the existing Python API's return value: the raw export.
                return destination
            finally:
                context.close()
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show Chromium for debugging (default: headless)")
    parser.add_argument("--timeout", type=int, default=180, metavar="SECONDS", help="Timeout per browser operation (default: 180)")
    parser.add_argument("--coastline-tolerance", type=float, default=3.0, metavar="MAP_UNITS", help="Maximum RDP tolerance, reduced where needed (default: 3)")
    parser.add_argument("--keep-geometry-source", action="store_true", help="Also retain raw cell coordinates for offline reprocessing/debugging")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        generate_world(headless=not args.headed, timeout_seconds=args.timeout,
                       coastline_tolerance=args.coastline_tolerance, keep_geometry_source=args.keep_geometry_source)
        return 0
    except PlaywrightTimeoutError as error:
        print(f"Timed out waiting for Azgaar: {error}\nTry --headed or increase --timeout.", file=sys.stderr)
    except (PlaywrightError, OSError, ValueError, RuntimeError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
