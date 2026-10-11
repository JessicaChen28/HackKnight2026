"""Generate, validate, and atomically publish an Azgaar Minimal JSON world."""

import argparse
import importlib.util
import math
from pathlib import Path
import sys
from urllib.parse import urlsplit

# Give people running the CLI from Conda (or another Python installation) a
# useful fix before third-party imports can produce a traceback.
if __name__ == "__main__":
    missing = [package for package in ("playwright", "shapely", "numpy", "psutil")
               if importlib.util.find_spec(package) is None]
    if missing:
        print(f"Missing Python packages: {', '.join(missing)}\n"
              f"This command is using: {sys.executable}\n"
              "Install dependencies into that same environment from the project directory:\n"
              "  python -m pip install -r requirements.txt\n"
              "  python -m playwright install chromium\n"
              "Then run: python generate_world.py", file=sys.stderr)
        raise SystemExit(1)

from runtime_paths import configure_playwright_browser, default_cli_export_root, playwright_browser_error

# Must be set before Playwright is imported, including in the spawned worker.
configure_playwright_browser()

from playwright.sync_api import sync_playwright
from shapely.errors import GEOSException

from geometry_source import extract_geometry
from generation_runtime import (ACTIVE_GENERATION, GenerationError, GenerationCancelled,
                                browser_environment, run_worker, safe_message)
from safe_storage import (DEFAULT_MAX_BYTES, checked_path, check_limit, generation_lock,
                          prepare_root, publish_file, publish_world, read_json,
                          staging_directory, write_json)
from world_parser import parse_world, process_world, validate_minimal

URL = "https://azgaar.github.io/Fantasy-Map-Generator/"
EXPORTS = default_cli_export_root()
MINIMAL_BUTTON = "#exportMapData button[onclick=\"exportToJson('Minimal')\"]"
GENERATION_LISTENER = """
window.__azgaarGenerated = null;
window.addEventListener('map:generated', event => {
    window.__azgaarGenerated = event.detail;
});
"""


class WorldProcessingError(GenerationError):
    """A generated world's source data cannot produce validated output."""


def validate_export(path, max_bytes=DEFAULT_MAX_BYTES):
    data = read_json(path, max_bytes)
    validate_minimal(data)
    parse_world(data)
    return data


def allowed_url(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == "azgaar.github.io"
                and parts.port in (None, 443) and not parts.username and not parts.password
                and parts.path.startswith("/Fantasy-Map-Generator/"))
    except ValueError:
        return False


def guard_browser(context, page):
    def route_request(route):
        if route.request.is_navigation_request() and not allowed_url(route.request.url):
            route.abort("blockedbyclient")
        else:
            route.continue_()

    context.route("**/*", route_request)
    context.on("page", lambda popup: popup.close() if popup != page else None)
    page.on("dialog", lambda dialog: dialog.dismiss())
    page.on("framenavigated", lambda frame: page.close()
            if frame == page.main_frame and not allowed_url(frame.url) else None)


def require_origin(page):
    if not allowed_url(page.url):
        raise GenerationError("Azgaar left the permitted HTTPS application; generation stopped")


def generate_world(headless=True, timeout_seconds=180, coastline_tolerance=3.0,
                   keep_geometry_source=False, max_attempts=5, *, export_root=None,
                   max_input_bytes=DEFAULT_MAX_BYTES, total_timeout_seconds=300,
                   max_memory_mb=2048,
                   navigation_timeout_seconds=None, export_timeout_seconds=None,
                   geometry_timeout_seconds=None, cancel_event=None, progress=print):
    """Return the original export path in a completed world directory.

    Call from a background UI thread with a threading.Event for cancellation.
    The host application's entry point needs the usual __main__ guard on Windows.
    """
    browser_error = playwright_browser_error()
    if browser_error:
        raise GenerationError(browser_error)
    deadlines = {
        "timeout_seconds": timeout_seconds, "total_timeout_seconds": total_timeout_seconds,
        "navigation_timeout_seconds": navigation_timeout_seconds if navigation_timeout_seconds is not None else timeout_seconds,
        "export_timeout_seconds": export_timeout_seconds if export_timeout_seconds is not None else timeout_seconds,
        "geometry_timeout_seconds": geometry_timeout_seconds if geometry_timeout_seconds is not None else timeout_seconds,
    }
    for key, value in deadlines.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be positive and finite")
    if not math.isfinite(coastline_tolerance) or coastline_tolerance < 0:
        raise ValueError("Coastline tolerance must be finite and nonnegative")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
        raise ValueError("Maximum generation attempts must be between 1 and 20")
    check_limit(max_input_bytes)
    check_limit(max_memory_mb)
    if cancel_event is not None and cancel_event.is_set():
        raise GenerationCancelled("World generation cancelled")
    if not ACTIVE_GENERATION.acquire(blocking=False):
        raise GenerationError("World generation is already active")
    try:
        root = prepare_root(export_root if export_root is not None else EXPORTS)
        config = dict(deadlines, headless=headless, coastline_tolerance=coastline_tolerance,
                      keep_geometry_source=keep_geometry_source, max_attempts=max_attempts,
                      max_input_bytes=max_input_bytes, max_memory_mb=max_memory_mb, export_root=str(root))
        with generation_lock(root), staging_directory(root) as workspace:
            stage = checked_path(run_worker(config, workspace, cancel_event, progress), workspace)
            manifest = read_json(stage / "complete.json", 4096)
            if manifest.get("status") != "complete":
                raise GenerationError("Worker did not produce a complete world")
            for name in ("azgaar_minimal.json", "world.json", "coastlines.json"):
                if not checked_path(stage / name, workspace).is_file():
                    raise GenerationError("Worker output is missing a required file")
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled("World generation cancelled")
            metadata = read_json(stage / "world.json", max_input_bytes)["metadata"]
            directory = publish_world(stage, root, metadata["mapName"])
            if progress is not None:
                progress(f"Saved world files: {directory}")
            return directory / "azgaar_minimal.json"
    finally:
        ACTIVE_GENERATION.release()


def _generate_with_retries(config, workspace, phase):
    for attempt in range(1, config["max_attempts"] + 1):
        phase("starting generation", config["timeout_seconds"])
        print(f"World generation attempt {attempt}/{config['max_attempts']}", flush=True)
        stage = workspace / f"attempt-{attempt}"
        stage.mkdir()
        try:
            return _generate_world_once(config, stage, phase)
        except WorldProcessingError as error:
            write_json(stage / "validation_error.json", {"error": safe_message(error)})
            failed = prepare_root(Path(config["export_root"]) / ".failed")
            publish_world(stage, config["export_root"], "rejected-world", destination_root=failed)
            if attempt == config["max_attempts"]:
                raise WorldProcessingError(f"No valid world after {attempt} attempts: {error}") from error
            print(f"World rejected: {safe_message(error)}. Generating a fresh replacement...", flush=True)
    raise AssertionError("Unreachable generation state")


def _generate_world_once(config, directory, phase):
    timeout_ms = config["timeout_seconds"] * 1000
    downloads = directory.parent / ".downloads"
    downloads.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=config["headless"], timeout=timeout_ms, chromium_sandbox=True,
            downloads_path=str(downloads), env=browser_environment(directory.parent))
        try:
            context = browser.new_context(accept_downloads=True, permissions=[],
                                          service_workers="block", viewport={"width": 1440, "height": 900})
            try:
                context.add_init_script(GENERATION_LISTENER)
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(config["navigation_timeout_seconds"] * 1000)
                guard_browser(context, page)
                expected = {"enabled": False, "count": 0}

                def check_download(download):
                    expected["count"] += 1
                    if not expected["enabled"] or expected["count"] != 1:
                        download.cancel()

                page.on("download", check_download)
                phase("navigation", config["navigation_timeout_seconds"])
                print("Opening Azgaar; generating a new random world...", flush=True)
                page.goto(URL, wait_until="domcontentloaded")
                require_origin(page)
                phase("world generation", config["timeout_seconds"])
                page.wait_for_function("""() => window.__azgaarGenerated != null ||
                    document.querySelector('#errorBox') !== null""")
                if page.locator("#errorBox").count():
                    raise WorldProcessingError("Azgaar reported a generation error")
                page.wait_for_function("""() =>
                    Array.isArray(window.pack?.burgs) && window.pack.burgs.length > 0 &&
                    Array.isArray(window.pack?.states) && window.pack.states.length > 0""")
                page.locator("#loading").wait_for(state="hidden")
                require_origin(page)
                generated = page.evaluate("window.__azgaarGenerated")
                if not isinstance(generated, dict) or "seed" not in generated:
                    raise WorldProcessingError("Missing generation completion metadata")
                print("Generation complete. Opening Export...", flush=True)
                phase("Minimal JSON download", config["export_timeout_seconds"])
                if not page.locator("#exportButton").is_visible():
                    page.locator("#optionsTrigger").click()
                page.locator("#exportButton").click()
                page.locator("#exportMapData").wait_for(state="visible")
                print("Downloading Azgaar's Minimal JSON export...", flush=True)
                expected["enabled"] = True
                with page.expect_download(timeout=config["export_timeout_seconds"] * 1000) as pending:
                    page.locator(MINIMAL_BUTTON).click()
                expected["enabled"] = False
                download = pending.value
                if download.failure() is not None:
                    raise GenerationError("Minimal JSON download failed or was cancelled; retry generation")
                downloaded = download.path()  # waits for completion, supervised by parent deadline
                if downloaded is None or not 0 < Path(downloaded).stat().st_size <= config["max_input_bytes"]:
                    raise GenerationError("Download is empty or exceeds the configured input size limit")
                temporary = directory / "download.part"  # inside exclusively created staging directory
                destination = directory / "azgaar_minimal.json"
                try:
                    download.save_as(temporary)
                    try:
                        data = validate_export(temporary, config["max_input_bytes"])
                        if str(data["info"]["seed"]) != str(generated["seed"]):
                            raise ValueError("Download seed differs from the generated world")
                    except ValueError as error:
                        raise WorldProcessingError(str(error)) from error
                    publish_file(temporary, destination)
                    del data
                finally:
                    temporary.unlink(missing_ok=True)
                print("Minimal JSON download completed and validated.", flush=True)
                phase("geometry extraction", config["geometry_timeout_seconds"])
                require_origin(page)
                print("Extracting geographic cell coordinates from this world...", flush=True)
                source = extract_geometry(page)
                source_path = directory / "geometry_source.json"
                write_json(source_path, source)
                if source_path.stat().st_size > config["max_input_bytes"]:
                    raise WorldProcessingError("Geometry source exceeds the configured input size limit")
                phase("geometry processing", config["geometry_timeout_seconds"])
                print("Filtering world data and simplifying coastlines...", flush=True)
                try:
                    process_world(destination, source, config["coastline_tolerance"],
                                  max_input_bytes=config["max_input_bytes"])
                except (ValueError, KeyError, TypeError, GEOSException) as error:
                    raise WorldProcessingError(str(error)) from error
                if not config["keep_geometry_source"]:
                    source_path.unlink()
                return directory
            finally:
                context.close()
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout", type=float, default=180, help="Browser operation timeout in seconds")
    parser.add_argument("--total-timeout", type=float, default=300, help="Deadline including all retries (seconds)")
    parser.add_argument("--navigation-timeout", type=float)
    parser.add_argument("--export-timeout", type=float)
    parser.add_argument("--geometry-timeout", type=float)
    parser.add_argument("--max-input-mb", type=int, default=64)
    parser.add_argument("--max-memory-mb", type=int, default=2048, help="Worker/browser combined RSS budget (MiB)")
    parser.add_argument("--export-root", type=Path)
    parser.add_argument("--coastline-tolerance", type=float, default=3.0)
    parser.add_argument("--keep-geometry-source", action="store_true")
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args()
    try:
        generate_world(headless=not args.headed, timeout_seconds=args.timeout,
                       coastline_tolerance=args.coastline_tolerance,
                       keep_geometry_source=args.keep_geometry_source, max_attempts=args.attempts,
                       export_root=args.export_root, max_input_bytes=args.max_input_mb * 1024 * 1024,
                       max_memory_mb=args.max_memory_mb,
                       total_timeout_seconds=args.total_timeout,
                       navigation_timeout_seconds=args.navigation_timeout,
                       export_timeout_seconds=args.export_timeout,
                       geometry_timeout_seconds=args.geometry_timeout)
        return 0
    except (KeyboardInterrupt, GenerationCancelled):
        print("Cancelled.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Export failed: {safe_message(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
