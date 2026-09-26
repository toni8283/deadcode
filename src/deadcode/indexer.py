"""
indexer.py – aggregate per-file parse results into a whole-repo index.

Responsibilities
----------------
* Accept a list of file paths (from the scanner) and a repo root.
* Call ``parse_file`` for each file.
* Merge all ``FileIndex`` objects into a single ``RepoIndex``.
* Build the convenience lookup maps (``definitions``, ``name_to_defs``).
* Propagate ``__all__`` membership and wildcard-import flags so the
  analyser can make informed decisions.

The indexer is deliberately *dumb* about cross-file semantics.  It just
collects and organises raw parse results; the graph builder and analyser
apply the cross-file reasoning.
"""

from __future__ import annotations

from pathlib import Path

from deadcode.models import FileIndex, RepoIndex, SymbolDef
from deadcode.parser import parse_file
from deadcode.scanner import derive_module_name, scan_files


def build_index(repo_root: str | Path) -> tuple[RepoIndex, list[dict[str, str]]]:
    """
    Scan *repo_root*, parse every discovered ``.py`` file, and return a
    populated :class:`~deadcode.models.RepoIndex`.

    Parameters
    ----------
    repo_root:
        Path to the repository root to analyse.

    Returns
    -------
    index:
        The fully populated repo index.
    errors:
        List of ``{"file": ..., "error": ...}`` dicts for files that could not
        be scanned or parsed.  These are surfaced in the final
        ``AnalysisResult`` but do NOT abort analysis.
    """
    repo_root = Path(repo_root).resolve()
    index = RepoIndex(root=str(repo_root))
    errors: list[dict[str, str]] = []

    # ----------------------------------------------------------------
    # 1. Discover files
    # ----------------------------------------------------------------
    py_files, scan_errors = scan_files(repo_root)
    for path_str, err_msg in scan_errors:
        errors.append({"file": path_str, "error": err_msg})

    # ----------------------------------------------------------------
    # 2. Determine the *package root* – the directory that contains the
    #    top-level Python packages.  We look for a ``src`` layout first
    #    (src/pkg/__init__.py) and fall back to the repo root.
    # ----------------------------------------------------------------
    package_root = _find_package_root(repo_root, py_files)

    # ----------------------------------------------------------------
    # 3. Parse each file
    # ----------------------------------------------------------------
    for file_path in py_files:
        module_name = derive_module_name(file_path, package_root)
        file_index = parse_file(file_path, module_name)
        index.files.append(file_index)

        if file_index.parse_error:
            errors.append(
                {"file": file_index.path, "error": file_index.parse_error}
            )

    # ----------------------------------------------------------------
    # 4. Build lookup maps
    # ----------------------------------------------------------------
    _build_lookup_maps(index)

    return index, errors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_package_root(repo_root: Path, py_files: list[Path]) -> Path:
    """
    Heuristically determine the package root.

    Strategy
    --------
    1. If ``repo_root/src/`` exists and contains at least one ``__init__.py``,
       use ``repo_root/src`` as the package root.
    2. If ``repo_root/src/`` exists but has no ``__init__.py`` packages,
       fall back to ``repo_root``.
    3. Otherwise use ``repo_root``.
    """
    src_dir = repo_root / "src"
    if src_dir.is_dir():
        # Check if any py file lives under src/
        for f in py_files:
            try:
                f.relative_to(src_dir)
                return src_dir
            except ValueError:
                continue
    return repo_root


def _build_lookup_maps(index: RepoIndex) -> None:
    """
    Populate ``index.definitions`` (qualified_name → SymbolDef) and
    ``index.name_to_defs`` (bare name → list[SymbolDef]).

    If two symbols share the same qualified name (e.g. two top-level functions
    with the same name in different files after a naming collision), the later
    one wins in ``definitions`` but both appear in ``name_to_defs``.
    """
    for file_index in index.files:
        for defn in file_index.definitions:
            # qualified_name map (last-writer wins for duplicates)
            index.definitions[defn.qualified_name] = defn

            # bare name map
            index.name_to_defs.setdefault(defn.name, []).append(defn)
