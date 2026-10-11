"""Main window, navigation, and one-at-a-time Qt background tasks."""

from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from desktop.screens import CharacterCreationScreen, GameplayScreen, MainMenuScreen, SettingsScreen
from desktop.services import (AzgaarWorldProvider, CampaignIntro, GameError, GameService,
                              GameSession, MockGameService, PhaseTwoCampaignService,
                              SampleWorldProvider, WorldProvider)
from desktop.storage import ApiKeyVault, GameSaveStore, Preferences


Task = Callable[[Event, Callable[[str], None]], object]


class TaskWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    progressed = Signal(str)
    done = Signal()

    def __init__(self, task: Task, cancel: Event):
        super().__init__()
        self.task = task
        self.cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            self.succeeded.emit(self.task(self.cancel, self.progressed.emit))
        except (GameError, ValueError) as error:
            self.failed.emit(str(error)[:500])
        except Exception as error:
            # Do not display a traceback or raw third-party exception text: a
            # future service exception might contain a request/API credential.
            self.failed.emit(f"Operation failed ({type(error).__name__}). Please retry.")
        finally:
            self.done.emit()


class MainWindow(QMainWindow):
    def __init__(self, *, preferences: Preferences | None = None,
                 vault: ApiKeyVault | None = None,
                 world_provider: WorldProvider | None = None,
                 sample_provider: WorldProvider | None = None,
                 game_service: GameService | None = None,
                 demo_game_service: GameService | None = None):
        super().__init__()
        self.setWindowTitle("SOLAR RPG")
        self.resize(1120, 760)
        self.preferences = preferences or Preferences()
        self.vault = vault or ApiKeyVault(self.preferences.settings)
        self.world_provider = world_provider or AzgaarWorldProvider()
        self.sample_provider = sample_provider or SampleWorldProvider()
        # Phase 2A owns only the campaign opening. Character creation and play
        # intentionally remain mock placeholders until their Phase 2 services
        # arrive. The vault supplies the key in memory, never via a save file.
        self.game_service = game_service or PhaseTwoCampaignService(
            lambda: self.vault.session.get("gemini", ""))
        self.demo_game_service = demo_game_service or MockGameService()
        self._active_game_service: GameService = self.game_service
        self.save_store = GameSaveStore(self.preferences)
        self.intro: CampaignIntro | None = None
        self.session: GameSession | None = None
        self._thread: QThread | None = None
        self._worker: TaskWorker | None = None
        self._cancel: Event | None = None
        self._callback: Callable[[object], None] | None = None
        self._closing = False

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.menu = MainMenuScreen()
        self.creation = CharacterCreationScreen()
        self.game = GameplayScreen()
        self.settings_screen = SettingsScreen()
        for page in (self.menu, self.creation, self.game, self.settings_screen):
            self.stack.addWidget(page)
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 5, 20, 10)
        self.message = QLabel("Ready.")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setObjectName("statusMessage")
        self.message.setWordWrap(True)
        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)
        self.spinner.setFixedWidth(120)
        self.spinner.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.hide()
        footer.addWidget(self.message, 1)
        footer.addWidget(self.spinner)
        footer.addWidget(self.cancel_button)
        outer.addLayout(footer)
        self.setCentralWidget(root)

        self._controls = [
            self.menu.new_button, self.menu.demo_button, self.menu.load_button,
            self.menu.settings_button, self.menu.exit_button,
            self.creation.back_button, self.creation.create_button,
            self.game.menu_button, self.game.save_button, self.game.send_button,
            self.settings_screen.back_button, self.settings_screen.save_button,
            self.settings_screen.save_gemini_button, self.settings_screen.clear_gemini_button,
            self.settings_screen.choose_exports, self.settings_screen.choose_saves,
        ]
        self.menu.new_button.clicked.connect(lambda: self.start_campaign(self.world_provider, self.game_service))
        self.menu.demo_button.clicked.connect(lambda: self.start_campaign(self.sample_provider, self.demo_game_service))
        self.menu.load_button.clicked.connect(self.choose_game)
        self.menu.settings_button.clicked.connect(self.open_settings)
        self.menu.exit_button.clicked.connect(self.close)
        self.creation.back_button.clicked.connect(self.show_menu)
        self.creation.create_button.clicked.connect(self.create_character)
        self.game.menu_button.clicked.connect(self.show_menu)
        self.game.save_button.clicked.connect(self.save_game)
        self.game.send_button.clicked.connect(self.take_action)
        self.settings_screen.back_button.clicked.connect(self.show_menu)
        self.settings_screen.save_button.clicked.connect(self.save_settings)
        self.settings_screen.save_gemini_button.clicked.connect(self.save_gemini_key)
        self.settings_screen.clear_gemini_button.clicked.connect(self.clear_gemini_key)
        self.settings_screen.choose_exports.clicked.connect(lambda: self.choose_folder(self.settings_screen.export_root))
        self.settings_screen.choose_saves.clicked.connect(lambda: self.choose_folder(self.settings_screen.save_root))
        self.cancel_button.clicked.connect(self.cancel_operation)
        if self.vault.warnings:
            self._show_error(" ".join(self.vault.warnings))

    def _set_busy(self, busy: bool, label: str = "") -> None:
        for control in self._controls:
            control.setEnabled(not busy)
        self.spinner.setVisible(busy)
        self.cancel_button.setVisible(busy)
        if busy:
            self.cancel_button.setEnabled(True)
        if label:
            self._show_message(label)

    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.message.setProperty("error", False)
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)

    def _show_error(self, text: str) -> None:
        self.message.setText(text)
        self.message.setProperty("error", True)
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)

    def _run_task(self, label: str, task: Task, callback: Callable[[object], None]) -> bool:
        if self._thread is not None:
            self._show_message("An operation is already running.")
            return False
        self._cancel = Event()
        self._callback = callback
        thread = QThread(self)
        worker = TaskWorker(task, self._cancel)
        worker.moveToThread(thread)
        self._thread, self._worker = thread, worker
        thread.started.connect(worker.run)
        worker.progressed.connect(self._show_message)
        worker.succeeded.connect(self._task_succeeded)
        worker.failed.connect(self._show_error)
        worker.done.connect(thread.quit)
        thread.finished.connect(self._task_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._set_busy(True, label)
        thread.start()
        return True

    @Slot(object)
    def _task_succeeded(self, value: object) -> None:
        if self._callback is not None and not self._closing:
            try:
                self._callback(value)
            except Exception:
                self._show_error("Could not display the result. Please retry.")

    @Slot()
    def _task_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._cancel = None
        self._callback = None
        self._set_busy(False)
        if self._closing:
            QTimer.singleShot(0, self.close)

    def cancel_operation(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.cancel_button.setEnabled(False)
            self._show_message("Cancelling…")

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            self._closing = True
            self.cancel_operation()
            event.ignore()
        else:
            event.accept()

    def show_menu(self) -> None:
        if self._thread is None:
            self.stack.setCurrentWidget(self.menu)
            self._show_message("Ready.")

    def start_campaign(self, provider: WorldProvider, service: GameService | None = None) -> None:
        if self._thread is not None:
            return
        export_root = self.preferences.export_root()
        active_service = service or self.game_service

        def task(cancel: Event, progress: Callable[[str], None]) -> CampaignIntro:
            world = provider.generate(export_root, cancel, progress)
            progress("Preparing the offline campaign..." if active_service is self.demo_game_service
                     else "Initializing your campaign with Gemini...")
            return active_service.initialize_campaign(world, cancel)

        if self._run_task("Preparing a world...", task, self._show_intro):
            self._active_game_service = active_service

    def _show_intro(self, value: object) -> None:
        assert isinstance(value, CampaignIntro)
        self.intro = value
        self.creation.show_intro(value, is_offline_demo=self._active_game_service is self.demo_game_service)
        self.stack.setCurrentWidget(self.creation)
        self._show_message(f"Campaign ready: {value.world.name}")

    def create_character(self) -> None:
        if self.intro is None or self._thread is not None:
            return
        description = self.creation.description.toPlainText()
        intro = self.intro
        self._run_task("Creating character...",
                       lambda cancel, progress: self._active_game_service.create_character(intro, description, cancel),
                       self._show_session)

    def _show_session(self, value: object) -> None:
        assert isinstance(value, GameSession)
        self.session = value
        self.game.show_session(value)
        self.stack.setCurrentWidget(self.game)
        self._show_message(f"Now exploring {value.world.name}.")

    def take_action(self) -> None:
        if self.session is None or self._thread is not None:
            return
        action, session = self.game.action.toPlainText(), self.session
        self._run_task("Considering your action...",
                       lambda cancel, progress: self._active_game_service.perform_action(session, action, cancel),
                       self._show_session)

    def save_game(self) -> None:
        if self.session is None or self._thread is not None:
            return
        session = self.session
        save_root = self.preferences.save_root()
        self._run_task("Saving game...",
                       lambda cancel, progress: self.save_store.save(session, save_root),
                       lambda path: self._show_message(f"Saved game: {Path(path).name}"))

    def choose_game(self) -> None:
        if self._thread is not None:
            return
        selected, _ = QFileDialog.getOpenFileName(self, "Load Game", str(self.preferences.save_root()),
                                                   "Game saves (*.json)")
        if selected:
            self.load_game_from_path(Path(selected))

    def load_game_from_path(self, path: Path) -> None:
        self._run_task("Loading game...",
                       lambda cancel, progress: self.save_store.load(path), self._show_session)

    def open_settings(self) -> None:
        if self._thread is not None:
            return
        screen = self.settings_screen
        screen.gemini_key.setText(self.vault.load_key("gemini"))
        screen.elevenlabs_key.setText(self.vault.session["elevenlabs"])
        screen.remember_elevenlabs.setChecked(self.vault.is_saved("elevenlabs"))
        screen.audio_enabled.setChecked(self.preferences.audio_enabled())
        screen.volume.setValue(self.preferences.audio_volume())
        screen.export_root.setText(str(self.preferences.export_root()))
        screen.save_root.setText(str(self.preferences.save_root()))
        self.stack.setCurrentWidget(screen)

    def choose_folder(self, target) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose Folder", target.text())
        if selected:
            target.setText(selected)

    def save_gemini_key(self) -> None:
        try:
            self.vault.save_key("gemini", self.settings_screen.gemini_key.text())
        except ValueError as error:
            self._show_error(str(error))
        except Exception:
            self._show_error("Could not save the Gemini key in the OS credential manager.")
        else:
            self._show_message("Gemini API key saved securely.")

    def clear_gemini_key(self) -> None:
        try:
            self.vault.clear_key("gemini")
        except Exception:
            self._show_error("Could not clear the Gemini key from the OS credential manager.")
        else:
            self.settings_screen.gemini_key.clear()
            self._show_message("Gemini API key cleared.")

    def save_settings(self) -> None:
        if self._thread is not None:
            return
        screen = self.settings_screen
        try:
            for provider, checked, value in (
                ("ElevenLabs", screen.remember_elevenlabs.isChecked(), screen.elevenlabs_key.text()),
            ):
                if checked and not value.strip():
                    raise ValueError(f"Enter a {provider} key before choosing Save key.")
            self.preferences.update(screen.export_root.text(), screen.save_root.text(),
                                    screen.audio_enabled.isChecked(), screen.volume.value())
            self.vault.set_key("elevenlabs", screen.elevenlabs_key.text(), screen.remember_elevenlabs.isChecked())
        except ValueError as error:
            self._show_error(str(error))
            return
        except Exception:
            # Credential-manager exceptions may include implementation details;
            # never place the exception text or API key in the UI or logs.
            self._show_error("Could not save one or more settings. Check the paths and OS credential manager.")
            return
        self._show_message("Settings saved.")
        self.stack.setCurrentWidget(self.menu)
