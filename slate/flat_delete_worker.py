"""Delete a flat directory through pinned Linux directory descriptors."""

import os
from pathlib import Path
import stat
import sys


def _delete_flat_directory(path: str) -> None:
    """Reject links in the directory chain and unlink only direct children."""

    # 2026-11-05: keep all pathname resolution outside GTK and pin the actual
    # directory. A concurrent symlink replacement must never redirect deletion.
    target = Path(path)
    if not target.is_absolute() or not target.name or ".." in target.parts:
        raise ValueError("Deletion requires an absolute directory path")
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    directory_fd = None
    try:
        for component in target.parent.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        directory_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=parent_fd)
        identity = os.fstat(directory_fd)
        with os.scandir(directory_fd) as entries:
            names = []
            # Inspect every child before removing any, preserving the rule that
            # real subdirectories make the entire operation inapplicable.
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    raise ValueError("The directory contains other directories and cannot be deleted.")
                names.append(entry.name)
        for name in names:
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError("The directory changed during deletion")
            # unlink never descends into a raced-in directory or follows a
            # child symlink; dir_fd also protects the parent pathname race.
            os.unlink(name, dir_fd=directory_fd)
        current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError("The directory changed during deletion")
        os.rmdir(target.name, dir_fd=parent_fd)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(parent_fd)


def _main() -> int:
    """Return a concise operation error to the asynchronous parent process."""

    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected one directory path")
        _delete_flat_directory(sys.argv[1])
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
