"""Detect the supported version-control system at a project root."""

from pathlib import Path


def is_normal_repository(project_root: str | Path, scm_type: str) -> bool:
    """Reject empty marker directories that are not real working copies."""

    marker = Path(project_root) / f".{scm_type}"
    if not marker.is_dir() or marker.is_symlink():
        return False
    # 2026-08-17: Codex/infrastructure may create an empty .git directory;
    # git init always creates both HEAD and config in a normal working copy.
    if scm_type == "git":
        return (marker / "HEAD").is_file() and (marker / "config").is_file()
    if scm_type == "hg":
        return (marker / "requires").is_file()
    return False
