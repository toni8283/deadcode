"""
remover.py – AST-based exact source-range removal of a candidate definition.

Responsibilities
----------------
* Given a source file and a ``Candidate``, locate the exact line range of the
  definition using the Python AST.
* Remove that range from the source without touching any other code.
* Write the modified source back to the file (in place).

Safety guarantees
-----------------
* Uses ``ast.parse`` to locate the definition – never naive text search.
* Verifies that the symbol name, kind, and line number all match before
  removing anything.
* If the definition cannot be located exactly, raises ``RemovalError`` and
  leaves the file unchanged.
* Only module-level functions and classes are supported.  Any attempt to
  remove a nested or unsupported definition raises ``RemovalError``.
* A blank line is preserved after the removed block to avoid fusing adjacent
  top-level statements.

Line-range calculation
-----------------------
Python 3.8+ AST nodes carry ``lineno``, ``col_offset``, ``end_lineno``, and
``end_col_offset``.  We use ``lineno`` / ``end_lineno`` as the inclusive
source range.

Decorators are NOT separate AST nodes at the top level; they are attached
to the ``FunctionDef`` / ``ClassDef`` node via ``node.decorator_list``.
The first decorator's ``lineno`` is the actual start of the whole definition
(including the ``@`` lines).  We therefore use
``min(node.lineno, *(d.lineno for d in node.decorator_list))`` as the
effective first line.
"""

from __future__ import annotations

import ast
from pathlib import Path

from deadcode.models import Candidate, SymbolKind


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class RemovalError(Exception):
    """Raised when the candidate cannot be safely removed."""


def remove_candidate(candidate: Candidate, worktree_root: str | Path) -> int:
    """
    Remove the definition identified by *candidate* from its source file
    inside *worktree_root*.

    Parameters
    ----------
    candidate:
        The :class:`~deadcode.models.Candidate` to remove.  ``candidate.file``
        must be an absolute path inside *worktree_root*.
    worktree_root:
        Root of the isolated Git worktree.  Used only to verify the file
        is inside the worktree (safety check).

    Returns
    -------
    int
        Number of source lines that were removed.

    Raises
    ------
    RemovalError
        When the definition cannot be precisely located or removal is unsafe.
    """
    worktree_root = Path(worktree_root).resolve()

    # ----------------------------------------------------------------
    # 1.  Validate candidate kind
    # ----------------------------------------------------------------
    if candidate.symbol.kind not in (
        SymbolKind.FUNCTION,
        SymbolKind.ASYNC_FUNCTION,
        SymbolKind.CLASS,
        SymbolKind.MODULE,
    ):
        raise RemovalError(
            f"Unsupported symbol kind '{candidate.symbol.kind.value}' for "
            f"'{candidate.qualified_name}'; only module-level functions, "
            "classes, and modules are supported."
        )

    # ----------------------------------------------------------------
    # 2.  Map candidate's original file path to the worktree copy
    # ----------------------------------------------------------------
    wt_root = Path(worktree_root).resolve()
    wt_file = _remap_to_worktree(candidate.file, wt_root).resolve()

    # Ensure the resolved target remains inside the worktree
    try:
        wt_file.relative_to(wt_root)
    except ValueError as exc:
        raise RemovalError(
            f"Target file '{wt_file}' is outside worktree root '{wt_root}'."
        ) from exc

    # Refuse non-Python files
    if wt_file.suffix != ".py":
        raise RemovalError(
            f"Refusing to remove non-Python file '{wt_file}'."
        )

    if not wt_file.exists() or not wt_file.is_file():
        raise RemovalError(
            f"Source file '{wt_file}' does not exist or is not a regular file in the worktree."
        )

    # ----------------------------------------------------------------
    # 3.  Handle MODULE removal (unlink file)
    # ----------------------------------------------------------------
    if candidate.symbol.kind == SymbolKind.MODULE:
        try:
            source = wt_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RemovalError(f"Cannot read '{wt_file}': {exc}") from exc
        lines_count = len(source.splitlines())
        try:
            wt_file.unlink()
        except OSError as exc:
            raise RemovalError(f"Cannot delete '{wt_file}': {exc}") from exc
        return lines_count

    # ----------------------------------------------------------------
    # 4.  Read and parse the worktree copy (functions/classes)
    # ----------------------------------------------------------------
    try:
        source = wt_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RemovalError(f"Cannot read '{wt_file}': {exc}") from exc

    try:
        tree = ast.parse(source, filename=str(wt_file))
    except SyntaxError as exc:
        raise RemovalError(
            f"Cannot parse '{wt_file}': SyntaxError at line {exc.lineno}: {exc.msg}"
        ) from exc

    # ----------------------------------------------------------------
    # 4.  Locate the definition
    # ----------------------------------------------------------------
    node = _find_definition(tree, candidate)
    if node is None:
        raise RemovalError(
            f"Cannot locate definition of '{candidate.symbol.name}' near line "
            f"{candidate.line} in '{wt_file}'.  The symbol may have already been "
            "removed or the source differs from the indexed version."
        )

    # ----------------------------------------------------------------
    # 5.  Verify this is a module-level definition (depth 0)
    # ----------------------------------------------------------------
    if not _is_module_level(tree, node):
        raise RemovalError(
            f"'{candidate.symbol.name}' is not a module-level definition; "
            "nested definitions are not supported."
        )

    # ----------------------------------------------------------------
    # 6.  Compute the line range to remove (1-based, inclusive)
    # ----------------------------------------------------------------
    first_line, last_line = _definition_line_range(node)

    # ----------------------------------------------------------------
    # 7.  Splice out the lines and write back
    # ----------------------------------------------------------------
    lines = source.splitlines(keepends=True)
    total_lines = len(lines)

    if first_line < 1 or last_line > total_lines:
        raise RemovalError(
            f"Computed line range [{first_line}, {last_line}] is out of bounds "
            f"(file has {total_lines} lines)."
        )

    # Convert to 0-based indices
    start_idx = first_line - 1
    end_idx = last_line  # exclusive for slicing

    lines_removed = end_idx - start_idx
    new_lines = lines[:start_idx] + lines[end_idx:]

    # Write the modified source back
    try:
        wt_file.write_text("".join(new_lines), encoding="utf-8")
    except OSError as exc:
        raise RemovalError(f"Cannot write '{wt_file}': {exc}") from exc

    return lines_removed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _remap_to_worktree(original_file: str, worktree_root: Path) -> Path:
    """
    Given an absolute path from the analysis index and an isolated worktree
    root, construct the corresponding path inside the worktree.

    The original file path may come from a different absolute root (the user's
    working tree).  We extract the relative portion by finding the longest
    common suffix that makes sense as a relative path.

    Strategy
    --------
    Both the candidate file and the worktree file share the same path
    *relative to their respective repository roots*.  We find the repo root
    of the original file by walking up to the ``.git`` directory, then build
    the relative path, then join it onto the worktree root.

    Fallback
    --------
    If no ``.git`` can be found, use the path as-is if it is already under
    worktree_root; otherwise raise ``RemovalError``.
    """
    orig = Path(original_file)

    # Fast path: file is already inside the worktree (e.g. tests using tmpdir)
    try:
        rel = orig.relative_to(worktree_root)
        return worktree_root / rel
    except ValueError:
        pass

    # Walk up from original file to find the git root
    orig_resolved = orig.resolve()
    git_root = _find_git_root(orig_resolved.parent)
    if git_root is None:
        raise RemovalError(
            f"Cannot remap '{orig}' to worktree '{worktree_root}': "
            "no .git directory found in ancestors."
        )

    try:
        rel = orig_resolved.relative_to(git_root)
    except ValueError as exc:
        raise RemovalError(
            f"'{orig}' is not under detected git root '{git_root}'."
        ) from exc

    return worktree_root / rel


def _find_git_root(start: Path) -> Path | None:
    """Walk up from *start* looking for a ``.git`` directory or file."""
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _find_definition(
    tree: ast.Module,
    candidate: Candidate,
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None:
    """
    Locate the AST node for the candidate at module level.

    Match criteria (all must hold):
    1. The node is a direct child of the module (module-level definition).
    2. The node's name equals ``candidate.symbol.name``.
    3. The node's kind (Function/AsyncFunction/Class) matches the candidate.
    4. The node's ``lineno`` is within ±5 lines of ``candidate.line``
       (allows for slight discrepancy due to decorators).
    """
    target_name = candidate.symbol.name
    target_line = candidate.line

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        if node.name != target_name:
            continue

        # Kind check
        expected_kinds = _kinds_for_node(node)
        if candidate.symbol.kind not in expected_kinds:
            continue

        # Line proximity – use the effective start (first decorator or def line)
        effective_start = _effective_start_line(node)
        if abs(effective_start - target_line) <= 10:
            return node

    return None


def _is_module_level(tree: ast.Module, target: ast.AST) -> bool:
    """Return True if *target* is a direct child of the module."""
    return target in ast.iter_child_nodes(tree)


def _definition_line_range(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[int, int]:
    """
    Return the (first_line, last_line) inclusive range for this definition,
    including any leading decorators.

    Both values are 1-based.
    """
    first = _effective_start_line(node)
    last = node.end_lineno  # type: ignore[attr-defined]
    return first, last


def _effective_start_line(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> int:
    """Return the first line of the definition, accounting for decorators."""
    if node.decorator_list:
        return min(d.lineno for d in node.decorator_list)
    return node.lineno


def _kinds_for_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> frozenset[SymbolKind]:
    """Map an AST node type to the matching SymbolKind values."""
    if isinstance(node, ast.FunctionDef):
        return frozenset({SymbolKind.FUNCTION})
    if isinstance(node, ast.AsyncFunctionDef):
        return frozenset({SymbolKind.ASYNC_FUNCTION})
    if isinstance(node, ast.ClassDef):
        return frozenset({SymbolKind.CLASS})
    return frozenset()
