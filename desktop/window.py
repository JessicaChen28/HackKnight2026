"""Main window, navigation, and one-at-a-time Qt background tasks."""

from pathlib import Path
import os
import shutil
import time
import traceback
from uuid import uuid4
from threading import Event
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from desktop.screens import CharacterCreationScreen, GameplayScreen, MainMenuScreen, SettingsScreen
from desktop.audio import AudioError, AudioPlayback, ElevenLabsAudioService
from desktop.services import (AzgaarWorldProvider, CampaignIntro, GameError, GameService,
                              GameSession, MockGameService, PhaseTwoCampaignService,
                              SampleWorldProvider, WorldProvider, WorldRef)
from desktop.storage import ApiKeyVault, GameSaveStore, Preferences


Task = Callable[[Event, Callable[[str], None]], object]


class CampaignPromotionError(GameError):
    """A completed campaign could not be published, but remains recoverable."""


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
            if os.environ.get("SOLAR_RPG_DEVELOPMENT") == "1":
                print(f"Desktop task exception: {type(error).__name__}: {str(error)[:500]}", flush=True)
                traceback.print_exc()
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
            self.vault.gemini_key)
        self.demo_game_service = demo_game_service or MockGameService()
        self.audio_service = ElevenLabsAudioService(self.vault_key_elevenlabs,
                                                     self.preferences.base / "audio-cache",
                                                     voice_id=self.preferences.audio_voice_id())
        # QMediaPlayer/QAudioOutput are parented to the GUI window and are
        # only driven by callbacks delivered on the GUI thread.
        self.audio_playback = AudioPlayback(self)
        self._active_game_service: GameService = self.game_service
        self.save_store = GameSaveStore(self.preferences)
        self.intro: CampaignIntro | None = None
        self.session: GameSession | None = None
        self._thread: QThread | None = None
        self._worker: TaskWorker | None = None
        self._cancel: Event | None = None
        self._callback: Callable[[object], None] | None = None
        self._closing = False
        self._pending_narration: tuple[str, str] | None = None

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
            self.settings_screen.save_elevenlabs_button, self.settings_screen.clear_elevenlabs_button,
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
        self.settings_screen.save_elevenlabs_button.clicked.connect(self.save_elevenlabs_key)
        self.settings_screen.clear_elevenlabs_button.clicked.connect(self.clear_elevenlabs_key)
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
        if self._pending_narration is not None and not self._closing:
            text, world_name = self._pending_narration
            self._pending_narration = None
            self._start_narration(text, world_name)
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
            self.audio_playback.stop()
            self.stack.setCurrentWidget(self.menu)
            self._show_message("Ready.")

    def start_campaign(self, provider: WorldProvider, service: GameService | None = None) -> None:
        if self._thread is not None:
            return
        export_root = self.preferences.export_root()
        active_service = service or self.game_service
        if isinstance(active_service, PhaseTwoCampaignService) and not active_service.has_key():
            self._show_error("Save a Gemini API key in Settings or set GEMINI_API_KEY before starting a new game.")
            return

        def task(cancel: Event, progress: Callable[[str], None]) -> CampaignIntro:
            # Gemini-backed games are built below a private, owned directory.
            # The resulting world directory is promoted only after all campaign
            # files validate and persist successfully.
            workspace = None
            succeeded = False
            preserve_workspace = False
            try:
                generation_root = export_root
                if isinstance(active_service, PhaseTwoCampaignService):
                    export_root.mkdir(parents=True, exist_ok=True)
                    workspace = export_root / f".solar-pending-{uuid4().hex}"
                    workspace.mkdir()
                    generation_root = workspace
                    progress("Generating world in a temporary campaign workspace...")
                world = provider.generate(generation_root, cancel, progress)
                progress("Preparing the offline campaign..." if active_service is self.demo_game_service
                         else "Initializing your campaign with Gemini...")
                intro = (active_service.initialize_campaign(world, cancel, progress)
                         if isinstance(active_service, PhaseTwoCampaignService)
                         else active_service.initialize_campaign(world, cancel))
                if workspace is not None:
                    try:
                        world = self._promote_workspace_world(world, workspace, export_root)
                        intro = CampaignIntro(world, intro.text)
                    except CampaignPromotionError:
                        preserve_workspace = True
                        raise
                succeeded = True
                return intro
            finally:
                if workspace is not None and not succeeded and not preserve_workspace:
                    self._cleanup_workspace(workspace, export_root)

        if self._run_task("Preparing a world...", task, self._show_intro):
            self._active_game_service = active_service

    def _show_intro(self, value: object) -> None:
        assert isinstance(value, CampaignIntro)
        self.intro = value
        self.creation.show_intro(value, is_offline_demo=self._active_game_service is self.demo_game_service)
        self.stack.setCurrentWidget(self.creation)
        self._show_message(f"Campaign ready: {value.world.name}")
        print("ElevenLabs narration: introduction received by desktop.", flush=True)
        if self.preferences.audio_enabled():
            self._pending_narration = (value.text, value.world.name)
        else:
            print("ElevenLabs narration: audio is disabled in Settings.", flush=True)

    @staticmethod
    def _cleanup_workspace(workspace: Path, export_root: Path) -> None:
        """Delete only an app-owned incomplete workspace beneath export_root."""
        try:
            root = export_root.resolve()
            candidate = workspace.resolve()
            if candidate.parent != root or not candidate.name.startswith(".solar-pending-"):
                raise ValueError("Refusing to clean an unsafe workspace path.")
            if candidate.exists():
                shutil.rmtree(candidate)
                print("Removed incomplete temporary campaign workspace.", flush=True)
        except Exception as error:
            print(f"Could not remove incomplete campaign workspace ({type(error).__name__}).", flush=True)

    @staticmethod
    def _promote_workspace_world(world: WorldRef, workspace: Path, export_root: Path) -> WorldRef:
        source = Path(world.directory or "").resolve()
        root, pending = export_root.resolve(), workspace.resolve()
        if source.parent != pending or not pending.name.startswith(".solar-pending-"):
            # Test and third-party providers written for the older interface may
            # return an already-owned directory. Do not delete or move it.
            try:
                pending.rmdir()
            except OSError:
                pass
            print("Campaign provider returned an external world directory; skipping workspace promotion.", flush=True)
            return world
        target = root / source.name
        if target.exists():
            raise CampaignPromotionError(f"Campaign promotion collision. Completed campaign remains at: {pending}")
        # Source and target are siblings under the same configured export root,
        # so os.replace is an atomic move when Windows/OneDrive permit it.
        # OneDrive can briefly hold a directory after campaign files are saved.
        last_error = None
        for attempt in range(1, 4):
            try:
                os.replace(source, target)
                last_error = None
                break
            except OSError as error:
                last_error = error
                print(f"Campaign promotion attempt {attempt}/3 failed: {type(error).__name__}, "
                      f"winerror={getattr(error, 'winerror', None)}, errno={getattr(error, 'errno', None)}, "
                      f"source={source}, target={target}", flush=True)
                if os.environ.get("SOLAR_RPG_DEVELOPMENT") == "1":
                    traceback.print_exc()
                time.sleep(0.25 * attempt)
        if last_error is not None:
            # Do not clean up: all validated files remain in this private
            # workspace for recovery or manual move after a OneDrive lock ends.
            raise CampaignPromotionError(
                f"Campaign initialized but could not be promoted (Windows error "
                f"{getattr(last_error, 'winerror', None) or getattr(last_error, 'errno', 'unknown')}). "
                f"Recoverable files remain at: {pending}") from last_error
        try:
            pending.rmdir()
        except OSError as error:
            # Promotion has succeeded. An empty pending directory is harmless;
            # do not turn a completed campaign into an error.
            print(f"Campaign promotion succeeded; empty workspace cleanup deferred "
                  f"(winerror={getattr(error, 'winerror', None)}, path={pending}).", flush=True)
        print(f"Promoted completed campaign: {target.name}", flush=True)
        return WorldRef(world.name, str(target), world.seed)

    def _start_narration(self, text: str, world_name: str) -> None:
        if not self.audio_service.api_key():
            self._show_message(f"Campaign ready: {world_name} (ElevenLabs key missing; text-only mode).")
            return
        def task(cancel: Event, progress: Callable[[str], None]):
            if cancel.is_set():
                print("ElevenLabs narration: worker cancelled before request.", flush=True)
                return None
            try:
                return self.audio_service.speak_narration(text)
            except AudioError:
                return None
        self._run_task("Generating narration audio...", task,
                       lambda path: path and self.audio_playback.play(path, self.preferences.audio_volume()))

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
                       lambda cancel, progress: (
                           self._active_game_service.perform_action(session, action, cancel, progress)
                           if isinstance(self._active_game_service, PhaseTwoCampaignService)
                           else self._active_game_service.perform_action(session, action, cancel)),
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
        screen.elevenlabs_voice_id.setText(self.preferences.audio_voice_id())
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

    def vault_key_elevenlabs(self) -> str:
        return self.vault.session["elevenlabs"] or os.environ.get("ELEVENLABS_API_KEY", "")

    def save_elevenlabs_key(self) -> None:
        try:
            self.vault.set_key("elevenlabs", self.settings_screen.elevenlabs_key.text(), True)
        except Exception:
            self._show_error("Could not save the ElevenLabs key in the OS credential manager.")
        else:
            self.settings_screen.remember_elevenlabs.setChecked(True)
            self._show_message("ElevenLabs API key saved securely.")

    def clear_elevenlabs_key(self) -> None:
        try:
            self.vault.clear_key("elevenlabs")
        except Exception:
            self._show_error("Could not clear the ElevenLabs key from the OS credential manager.")
        else:
            self.settings_screen.elevenlabs_key.clear()
            self.settings_screen.remember_elevenlabs.setChecked(False)
            self._show_message("ElevenLabs API key cleared.")

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
                                    screen.audio_enabled.isChecked(), screen.volume.value(),
                                    screen.elevenlabs_voice_id.text())
            self.vault.set_key("elevenlabs", screen.elevenlabs_key.text(), screen.remember_elevenlabs.isChecked())
            self.audio_service.voice_id = screen.elevenlabs_voice_id.text().strip() or self.audio_service.voice_id
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
