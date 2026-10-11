"""Launch the native fantasy RPG desktop application."""

import importlib.util
import multiprocessing
import sys
import faulthandler
import logging


def main() -> int:
    if __import__("os").environ.get("SOLAR_RPG_DEVELOPMENT") == "1":
        faulthandler.enable(all_threads=True)
    missing = [name for name in ("PySide6", "keyring") if importlib.util.find_spec(name) is None]
    if missing:
        print(f"Missing desktop dependencies: {', '.join(missing)}\n"
              "Install them with: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    from PySide6.QtWidgets import QApplication
    from desktop.storage import Preferences
    from desktop.window import MainWindow
    from runtime_paths import configure_file_logging

    app = QApplication(sys.argv)
    app.setOrganizationName("SOLARRPG")
    app.setApplicationName("SOLARRPG")
    preferences = Preferences()
    log_path = configure_file_logging(preferences.base)
    if log_path:
        logging.getLogger("solar_rpg").info("SOLAR RPG started (frozen=%s)", bool(getattr(sys, "frozen", False)))
    app.setStyleSheet("""
        QWidget { background: #171a20; color: #e9e2d3; font-family: 'Segoe UI'; font-size: 11pt; }
        QMainWindow, QStackedWidget { background: #171a20; }
        QLabel#screenTitle { color: #d7b36e; font-family: Georgia; font-size: 25pt; font-weight: bold; }
        QLabel#screenSubtitle, QLabel#mutedText { color: #aab0b3; }
        QLabel#statusMessage { color: #b7c2c4; padding: 4px; }
        QLabel#statusMessage[error="true"] { color: #e79d92; }
        QPushButton { background: #293541; color: #f1e7d5; border: 1px solid #816c49;
                      border-radius: 5px; padding: 8px 14px; }
        QPushButton:hover { background: #354959; border-color: #c8a46a; }
        QPushButton:disabled { background: #26292d; color: #737b80; border-color: #4b4b4b; }
        QPlainTextEdit, QLineEdit, QListWidget { background: #202730; color: #f3eee1;
                       border: 1px solid #58606a; border-radius: 5px; padding: 7px; }
        QPlainTextEdit:focus, QLineEdit:focus { border-color: #c8a46a; }
        QGroupBox { border: 1px solid #655b4a; border-radius: 6px; margin-top: 15px;
                    padding: 10px; color: #d7b36e; font-weight: bold; }
        QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
        QScrollArea { border: none; }
        QSlider::groove:horizontal { height: 5px; background: #45505a; }
        QSlider::handle:horizontal { background: #d7b36e; width: 15px; margin: -5px 0; }
    """)
    window = MainWindow(preferences=preferences)
    window.show()
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
