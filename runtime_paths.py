"""Paths and startup configuration shared by development and frozen builds.

Nothing writable belongs beside the executable.  The installer owns the bundle;
Qt's AppDataLocation owns saves, worlds, cache, settings, and logs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys


APP_NAME = "SOLAR RPG"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Return the PyInstaller extraction root or the source directory."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def bundled_browser_root() -> Path:
    return bundle_root() / "playwright-browsers"


def configure_playwright_browser() -> Path | None:
    """Use the browser copied into a frozen one-directory distribution.

    This runs before importing ``playwright``.  Development environments keep
    their normal Playwright-managed browser cache unless explicitly overridden.
    """
    browser_root = bundled_browser_root()
    if browser_root.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
        return browser_root
    return None


def playwright_browser_error() -> str | None:
    """Return a clear frozen-build diagnostic when Chromium was not packaged."""
    if is_frozen() and configure_playwright_browser() is None:
        return ("SOLAR RPG's bundled Chromium browser is missing. Reinstall SOLAR RPG "
                "or ask the distributor for a complete installer.")
    return None


def default_cli_export_root() -> Path:
    """Keep source-tree CLI output familiar while frozen builds use LocalAppData."""
    if not is_frozen():
        return Path(__file__).resolve().parent / "exports"
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "SOLARRPG" / "SOLAR RPG" / "worlds"


def configure_file_logging(app_data_root: Path) -> Path | None:
    """Create a per-user diagnostic log without redirecting ordinary UI output."""
    try:
        log_root = app_data_root / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        path = log_root / "solar-rpg.log"
        logger = logging.getLogger("solar_rpg")
        if not any(isinstance(handler, logging.FileHandler) and handler.baseFilename == str(path)
                   for handler in logger.handlers):
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        return path
    except OSError:
        return None
