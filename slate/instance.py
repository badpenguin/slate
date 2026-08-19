"""Single-instance process lock stored outside projects and persistent config."""

from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """Report that another process currently owns the application lock."""

    def __init__(self, pid: int | None) -> None:
        """Retain the diagnostic PID read from the locked runtime file."""

        self.pid = pid
        detail = f" (PID {pid})" if pid is not None else ""
        super().__init__(f"SLATE is already running{detail}")


class InstanceLock:
    """Hold an advisory Linux lock for the complete GUI process lifetime."""

    def __init__(self, descriptor: int, path: Path) -> None:
        """Retain the locked descriptor and its diagnostic runtime path."""

        self.descriptor = descriptor
        self.path = path

    @classmethod
    def acquire(cls, path: Path | None = None) -> "InstanceLock":
        """Acquire the process lock atomically or report the current owner PID."""

        lock_path = path or cls.default_path()
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            information = os.fstat(descriptor)
            if information.st_uid != os.getuid():
                raise PermissionError("the lock belongs to another user")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise AlreadyRunningError(cls._read_pid(descriptor)) from error
            # 2026-08-16: il lock viene acquisito soltanto dal processo GUI
            # finale, quindi questo PID coincide già con il proprietario reale.
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
            return cls(descriptor, lock_path)
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def default_path() -> Path:
        """Return a per-user runtime path that disappears at logout when possible."""

        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            return Path(runtime) / "slate.lock"
        return Path(tempfile.gettempdir()) / f"slate-{os.getuid()}.lock"

    @staticmethod
    def _read_pid(descriptor: int) -> int | None:
        """Read the advisory owner PID without trusting it for lock validity."""

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = os.read(descriptor, 64).decode("ascii").strip()
            return int(content) if content else None
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    def close(self) -> None:
        """Release the advisory lock and close its descriptor exactly once."""

        if self.descriptor < 0:
            return
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        os.close(self.descriptor)
        self.descriptor = -1
