"""
scanner.py – recursive Python source file discovery.

Responsibilities
----------------
* Walk a repository directory tree.
* Skip well-known non-source directories (virtualenvs, caches, build
  artefacts, VCS metadata, etc.).
* Yield only ``.py`` files.
* Never raise: all OS-level errors are captured and returned as a list of
  ``(path, error_message)`` pairs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Directories that are unconditionally ignored during scanning.
# The check is case-sensitive and applied to each *directory component* of
# the path, so e.g. any directory whose basename is ".git" is skipped no
# matter where it appears in the tree.
# ---------------------------------------------------------------------------
IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        "*.egg-info",  # handled specially below
        "site-packages",
        ".nox",
    }
)


def _is_ignored_dir(dirname: str) -> bool:
    """Return ``True`` if *dirname* (a single path component) should be skipped."""
    if dirname in IGNORED_DIR_NAMES:
        return True
    # Glob-style suffix matches for egg-info, dist-info, etc.
    if dirname.endswith(".egg-info") or dirname.endswith(".dist-info"):
        return True
    return False


def scan_files(root: str | Path) -> tuple[list[Path], list[tuple[str, str]]]:
    """
    Recursively discover all ``.py`` files under *root*, skipping ignored
    directories.

    Parameters
    ----------
    root:
        Absolute or relative path to the repository root to scan.

    Returns
    -------
    files:
        Sorted list of :class:`pathlib.Path` objects for discovered ``.py``
        files.
    errors:
        List of ``(path_str, error_message)`` tuples for any OS-level errors
        encountered while traversing the directory tree.
    """
    root = Path(root).resolve()
    files: list[Path] = []
    errors: list[tuple[str, str]] = []

    try:
        _walk(root, files, errors)
    except Exception as exc:  # pragma: no cover – defensive catch-all
        errors.append((str(root), f"Unexpected error during scan: {exc}"))

    files.sort()
    return files, errors


def _walk(directory: Path, files: list[Path], errors: list[tuple[str, str]]) -> None:
    """Recursive helper that populates *files* and *errors* in place."""
    try:
        entries = list(directory.iterdir())
    except PermissionError as exc:
        errors.append((str(directory), f"Permission denied: {exc}"))
        return
    except OSError as exc:
        errors.append((str(directory), str(exc)))
        return

    for entry in entries:
        if entry.is_symlink():
            # Do not follow symlinks to avoid cycles.
            continue

        if entry.is_dir():
            if _is_ignored_dir(entry.name):
                continue
            _walk(entry, files, errors)

        elif entry.is_file() and entry.suffix == ".py":
            files.append(entry)


# ---------------------------------------------------------------------------
# Module name derivation
# ---------------------------------------------------------------------------


def derive_module_name(file_path: Path, repo_root: Path) -> str:
    """
    Convert a filesystem path to a dotted Python module name relative to
    *repo_root*.

    Examples
    --------
    >>> derive_module_name(Path("/proj/src/pkg/utils.py"), Path("/proj/src"))
    'pkg.utils'
    >>> derive_module_name(Path("/proj/pkg/__init__.py"), Path("/proj"))
    'pkg'
    """
    try:
        rel = file_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        # File is outside repo_root somehow; fall back to stem only.
        return file_path.stem

    parts = list(rel.parts)
    if not parts:
        return file_path.stem

    # Strip .py suffix from the final part
    parts[-1] = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]

    # __init__ modules collapse to their package name
    if parts[-1] == "__init__" and len(parts) > 1:
        parts = parts[:-1]

    return ".".join(parts)
