"""Small local-storage primitives: bounded reads, no-clobber writes, staging.

Paths are checked for symlinks/reparse points. This is not a sandbox against
another process running as the same OS user and racing directory replacements.
"""

from contextlib import contextmanager
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
from uuid import uuid4

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_NODES = 2_000_000
MAX_DEPTH = 100


def safe_message(value):
    """Bound log messages and remove terminal control/format characters."""
    return "".join(c for c in str(value)[:600] if not unicodedata.category(c).startswith("C"))


def checked_path(path, root=None):
    path = Path(os.path.abspath(path))
    if root is not None:
        root = Path(os.path.abspath(root))
        if not path.is_relative_to(root):
            raise ValueError("Output path escapes the export root")
    for item in (*reversed(path.parents), path):
        try:
            details = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise ValueError("Symbolic links and Windows reparse points are not allowed in storage paths")
    return path


def prepare_root(path):
    root = checked_path(path)
    root.mkdir(parents=True, exist_ok=True)
    return checked_path(root)


def safe_name(value):
    value = unicodedata.normalize("NFKC", str(value or "world"))
    value = "".join("_" if unicodedata.category(c).startswith("C") else c for c in value)
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    while ".." in value:
        value = value.replace("..", "_")
    value = value[:80]
    value = value.encode("utf-8")[:160].decode("utf-8", errors="ignore").strip(" .") or "world"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "world_" + value
    return value


def validate_json_tree(data):
    pending = [(data, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("JSON exceeds the supported structure/depth limit")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON contains a nonfinite number")
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                raise ValueError("JSON object keys must be strings")
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((v, depth + 1) for v in item)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ValueError("Unsupported JSON value type")


def check_limit(max_bytes):
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("Maximum input bytes must be a positive integer")


def read_json(path, max_bytes=DEFAULT_MAX_BYTES):
    check_limit(max_bytes)
    path = checked_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("JSON input must be a regular file")
    if not 0 < before.st_size <= max_bytes:
        raise ValueError(f"JSON input must be nonempty and no larger than {max_bytes} bytes")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("JSON input changed while opening it")
        raw = stream.read(max_bytes + 1)
    if not raw or len(raw) > max_bytes:
        raise ValueError("JSON input is empty or exceeds its size limit")

    def reject_constant(value):
        raise ValueError("JSON contains a nonfinite numeric constant")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON contains duplicate object keys")
            result[key] = value
        return result

    try:
        data = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid UTF-8 JSON input (syntax or excessive nesting)") from error
    validate_json_tree(data)
    return data


def publish_file(temporary, destination):
    """Atomic no-replace publication on the same filesystem (NTFS/POSIX)."""
    temporary, destination = checked_path(temporary), checked_path(destination)
    os.link(temporary, destination)  # fails atomically if destination exists
    temporary.unlink()


def write_json(path, data):
    path = checked_path(path)
    validate_json_tree(data)
    fd, name = tempfile.mkstemp(prefix=".json-", suffix=".part", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        publish_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rename_directory(source, destination):
    """Atomic directory rename that MUST NOT replace even an empty directory."""
    source, destination = checked_path(source), checked_path(destination)
    if os.name == "nt":
        os.rename(source, destination)  # Windows fails if destination exists
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(os.fsencode(source), os.fsencode(destination), 4)
    else:
        raise OSError("This platform lacks atomic no-replace directory rename")
    if result:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError("World directory already exists")
        raise OSError(code, "Cannot publish world directory")


def remove_private_directory(path, root):
    path = checked_path(path, root)
    if path == checked_path(root):
        raise ValueError("Refusing to remove the export root")
    if not path.exists():
        return
    # Python 3.10+ rmtree unlinks descendant symlinks and does not descend into
    # Windows junctions. The top-level private directory was checked above.
    shutil.rmtree(path)


@contextmanager
def staging_directory(root, prefix=".staging-"):
    root = prepare_root(root)
    stage = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    try:
        yield stage
    finally:
        remove_private_directory(stage, root)


def publish_world(stage, root, display_name, *, destination_root=None):
    root = prepare_root(root)
    stage = checked_path(stage, root)
    parent = prepare_root(checked_path(destination_root, root)) if destination_root is not None else root
    destination = checked_path(parent / f"{safe_name(display_name)}_{uuid4().hex}", root)
    rename_directory(stage, destination)
    return destination


@contextmanager
def generation_lock(root):
    """Kernel lock released after crashes; the small lock file intentionally stays."""
    path = checked_path(prepare_root(root) / ".generation.lock", root)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    stream = os.fdopen(fd, "r+b", buffering=0)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            stream.write(b"0")
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise RuntimeError("A world generation operation is already active for this export root") from error
        yield
    finally:
        if locked:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        stream.close()
