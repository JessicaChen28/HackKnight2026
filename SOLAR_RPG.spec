# PyInstaller one-directory build specification for Windows.
# Build through packaging\build_windows.ps1 so Chromium is staged first.

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules


project = Path(SPECPATH)
browser_root = project / "build" / "playwright-browsers"
if not browser_root.is_dir():
    raise SystemExit(
        "Bundled Playwright Chromium is missing. Run packaging\\build_windows.ps1 first.")

datas, binaries, hiddenimports = [], [], []
# Do not collect every PySide6 module: that drags in unrelated QML/3D plugins
# and can fail where optional Qt assets are absent. PyInstaller's Qt hooks
# collect the platform and multimedia plugins needed by these specific modules.
hiddenimports += ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtMultimedia"]
for package in ("playwright",):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# Generated Gemini and ElevenLabs clients have many dynamic modules. Keep
# their code in the PYZ archive rather than copying every generated .py file
# into a deep directory tree that can exceed Windows installer path limits.
hiddenimports += collect_submodules("google.genai")
hiddenimports += collect_submodules("elevenlabs")
# Used by keyring on Windows and by Playwright's synchronous API at runtime.
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += ["playwright._impl._driver", "playwright.sync_api"]
datas += [(str(browser_root), "playwright-browsers")]

debug = os.environ.get("SOLAR_RPG_DEBUG_BUILD") == "1"
exe_name = "SOLAR RPG Debug" if debug else "SOLAR RPG"

a = Analysis(
    [str(project / "run_desktop.py")],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "unittest", "tests"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=debug,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=exe_name,
)
