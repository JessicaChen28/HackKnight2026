"""One supervised local worker, hard deadlines, cancellation, and process cleanup."""

from contextlib import redirect_stderr, redirect_stdout
import multiprocessing
import os
from pathlib import Path
import threading
import time

import psutil
from safe_storage import safe_message


class GenerationError(RuntimeError):
    pass


class GenerationCancelled(GenerationError):
    pass


class GenerationTimeout(GenerationError):
    pass


ACTIVE_GENERATION = threading.Lock()


def browser_environment(temp_directory):
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "HOME",
               "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
               "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "LANG", "LC_ALL",
               "PLAYWRIGHT_BROWSERS_PATH"}
    result = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    result.update(TEMP=str(temp_directory), TMP=str(temp_directory), TMPDIR=str(temp_directory))
    return result


class ProgressStream:
    def __init__(self, connection):
        self.connection = connection
        self.pending = ""

    def write(self, value):
        for part in str(value).splitlines(keepends=True):
            self.pending = (self.pending + part)[:600]
            if part.endswith("\n"):
                self.flush()
        return len(value)

    def flush(self):
        if self.pending:
            self.connection.send(("progress", safe_message(self.pending)))
            self.pending = ""


def worker_main(connection, stop, config, workspace):
    # Only this disposable worker's environment changes, never the host UI's.
    environment = browser_environment(workspace)
    os.environ.clear()
    os.environ.update(environment)
    from generate_world import _generate_with_retries
    stream = ProgressStream(connection)

    def phase(name, seconds):
        if stop.is_set():
            raise GenerationCancelled("World generation cancelled")
        connection.send(("phase", (name, seconds)))

    try:
        with redirect_stdout(stream), redirect_stderr(stream):
            result = _generate_with_retries(config, Path(workspace), phase)
        stream.flush()
        connection.send(("result", str(result)))
    except BaseException as error:
        # Detailed stage is conveyed separately; avoid arbitrary page payloads,
        # OS exception paths, or complete exception tracebacks in UI messages.
        message = "Storage operation failed; check free space and permissions" if isinstance(error, OSError) else safe_message(error)
        connection.send(("error", message))
    finally:
        connection.close()


def _remember_children(process, known):
    try:
        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            known[(child.pid, child.create_time())] = child
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _stop_tree(process, known):
    if process.pid is None:
        return
    _remember_children(process, known)
    targets = list(known.values())
    try:
        targets.append(psutil.Process(process.pid))
    except psutil.NoSuchProcess:
        pass
    for target in reversed(targets):
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=2)
    for target in alive:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=2)
    process.join(timeout=2)
    if alive:
        raise GenerationError("Could not stop all generation processes; close Chromium before retrying")


def _resident_bytes(process, known):
    total = 0
    try:
        targets = [psutil.Process(process.pid), *known.values()]
    except psutil.NoSuchProcess:
        targets = list(known.values())
    for target in targets:
        try:
            total += target.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total


def run_worker(config, workspace, cancel_event=None, progress=print, target=worker_main):
    """Bound all blocking work, including download.save_as/evaluate/GEOS calls."""
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    stop = ctx.Event()
    process = ctx.Process(target=target, args=(writer, stop, config, str(workspace)), daemon=False)
    known = {}
    deadline = time.monotonic() + config["total_timeout_seconds"]
    phase_name = "starting worker"
    phase_deadline = deadline
    result = None
    try:
        process.start()
        writer.close()
        while True:
            _remember_children(process, known)
            if _resident_bytes(process, known) > config.get("max_memory_mb", 2048) * 1024 * 1024:
                raise GenerationError("Generation exceeded the memory budget; retry or increase --max-memory-mb")
            download_bytes = 0
            for path in (Path(workspace) / ".downloads").glob("*"):
                try:
                    download_bytes += path.stat().st_size
                except FileNotFoundError:
                    pass  # Playwright may remove a completed download concurrently.
            if download_bytes > config.get("max_input_bytes", 64 * 1024 * 1024):
                raise GenerationError("Download exceeds the configured input size limit")
            if cancel_event is not None and cancel_event.is_set():
                stop.set()
                raise GenerationCancelled("World generation cancelled")
            now = time.monotonic()
            if now >= deadline or now >= phase_deadline:
                stop.set()
                raise GenerationTimeout(f"Timed out during {phase_name}; retry or increase the timeout")
            if reader.poll(0.1):
                try:
                    kind, value = reader.recv()
                except EOFError:
                    raise GenerationError(f"Worker stopped unexpectedly during {phase_name}") from None
                if kind == "phase":
                    phase_name, seconds = value
                    phase_deadline = min(deadline, time.monotonic() + seconds)
                elif kind == "progress" and progress is not None:
                    progress(safe_message(value))
                elif kind == "error":
                    raise GenerationError(f"{phase_name}: {safe_message(value)}")
                elif kind == "result":
                    result = Path(value)
                    break
            if not process.is_alive() and not reader.poll():
                raise GenerationError(f"Worker stopped unexpectedly during {phase_name}")
        process.join(timeout=3)
        return result
    finally:
        try:
            _stop_tree(process, known)
        finally:
            reader.close()
            writer.close()
            if process.pid is not None and not process.is_alive():
                process.close()
