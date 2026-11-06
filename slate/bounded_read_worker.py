"""Read bounded project contents through pinned no-follow Linux descriptors."""

import base64
import os
from pathlib import Path
import stat
import sys


def _read_project_file(root: str, path: str, limit: int) -> bytes:
    """Read only a regular file reached through a fixed directory chain inside root."""

    target = Path(path)
    project = Path(root)
    if not target.is_absolute() or not project.is_absolute() or ".." in target.parts or limit < 0:
        raise ValueError("Invalid project preview path or byte limit")
    target.relative_to(project)
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    file_fd = None
    try:
        # 2026-11-06: every directory is pinned before the next component is
        # opened. Replacement links must not redirect a preview outside root.
        for component in target.parent.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Preview requires a regular file")
        if info.st_size > limit:
            raise ValueError("File over 5 MiB: preview is unavailable")
        data = bytearray()
        # A second byte budget also protects against files growing after fstat.
        while len(data) <= limit:
            chunk = os.read(file_fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise ValueError("File over 5 MiB: preview is unavailable")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _main() -> int:
    """Return arbitrary file bytes safely through the UTF-8 subprocess transport."""

    try:
        if len(sys.argv) != 4:
            raise ValueError("Expected root, file path and byte limit")
        data = _read_project_file(sys.argv[1], sys.argv[2], int(sys.argv[3]))
        sys.stdout.buffer.write(base64.b64encode(data))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
