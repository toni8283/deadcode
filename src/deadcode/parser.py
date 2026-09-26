"""
parser.py – AST-level analysis of a single Python source file.

Responsibilities
----------------
* Parse a ``.py`` file into an AST using the standard-library ``ast`` module.
* Extract all *definitions* (functions, async functions, classes, module-level
  variables / assignments).
* Extract all *imports* (``import x``, ``from x import y [as z]``,
  ``from x import *``).
* Extract all *name references* (usages of bare names, calls, attribute
  access chains, base-class lists, decorator names, etc.).
* Detect ``__all__`` assignments so the caller can mark exported symbols.
* Handle syntax errors gracefully – return a ``FileIndex`` with
  ``parse_error`` set rather than raising.

Design note on completeness vs. soundness
------------------------------------------
We deliberately collect *more* references than strictly necessary (e.g. every
``Name`` node, not just the ones that resolve to top-level defs).  False
positives in the reference set (marking something ACTIVE when it might not be)
are safe – they cause us to miss dead code.  False negatives (missing a
reference) would cause us to delete live code, which is unsafe.  Therefore we
err on the side of inclusion and rely on the Analyzer's fail-closed rules to
escalate ambiguous cases to REVIEW.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from deadcode.models import (
    FileIndex,
    ImportRecord,
    Location,
    SymbolDef,
    SymbolKind,
    SymbolRef,
)

# ---------------------------------------------------------------------------
# Patterns that hint at dynamic / reflective access which forces REVIEW
# ---------------------------------------------------------------------------
_DYNAMIC_CALL_NAMES: frozenset[str] = frozenset(
    {
        "getattr",
        "setattr",
        "hasattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "eval",
        "exec",
        "importlib",
        "__import__",
        "importlib.import_module",
    }
)

# Names that, when assigned a string, may reference symbols dynamically
_STRING_REF_MARKERS: frozenset[str] = frozenset(
    {
        "entry_points",
        "plugin",
        "register",
        "route",
    }
)


# ---------------------------------------------------------------------------
# Visitor
# ---------------------------------------------------------------------------


class _FileVisitor(ast.NodeVisitor):
    """
    Walk the AST of one file and populate lists of definitions, imports, and
    references.

    Parameters
    ----------
    module_name:
        Dotted module name for this file (used to build qualified names).
    file_path:
        Absolute path string (stored in ``Location`` objects).
    """

    def __init__(self, module_name: str, file_path: str) -> None:
        self._module = module_name
        self._file = file_path

        self.definitions: list[SymbolDef] = []
        self.imports: list[ImportRecord] = []
        self.references: list[SymbolRef] = []
        self.all_names: list[str] | None = None
        self.has_wildcard_import: bool = False

        # Depth tracker so we can distinguish top-level vs nested defs
        self._scope_depth: int = 0
        # Names defined in the current scope (used to skip self-refs while
        # inside nested functions / classes)
        self._scope_stack: list[str] = []
        # Track whether a dynamic call was seen inside the current function scope
        self._current_scope_has_dynamic: list[bool] = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _loc(self, node: ast.AST) -> Location:
        return Location(file=self._file, line=getattr(node, "lineno", 0))

    def _qual(self, name: str) -> str:
        """Build a qualified name by joining the module prefix with *name*."""
        return f"{self._module}.{name}" if self._module else name

    def _nested_qual(self, name: str) -> str:
        """Build a qualified name including the current scope stack."""
        parts = [self._module] + self._scope_stack + [name]
        return ".".join(p for p in parts if p)

    @staticmethod
    def _is_dunder(name: str) -> bool:
        return name.startswith("__") and name.endswith("__")

    @staticmethod
    def _decorator_name(node: ast.expr) -> str:
        """Extract a printable name from a decorator expression."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return ast.unparse(node)
        if isinstance(node, ast.Call):
            return _FileVisitor._decorator_name(node.func)
        return "<decorator>"

    # ------------------------------------------------------------------
    # Definition visitors
    # ------------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, async_=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, async_=True)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        async_: bool,
    ) -> None:
        name = node.name
        has_decorator = bool(node.decorator_list)
        is_dunder = self._is_dunder(name)
        kind = SymbolKind.ASYNC_FUNCTION if async_ else SymbolKind.FUNCTION

        # Record the definition (top-level and class-method level)
        defn = SymbolDef(
            name=name,
            qualified_name=self._nested_qual(name),
            kind=kind,
            location=self._loc(node),
            is_private=name.startswith("_"),
            has_decorator=has_decorator,
            is_dunder=is_dunder,
        )
        self.definitions.append(defn)

        # Decorators are *references* to names defined elsewhere
        for dec in node.decorator_list:
            dec_name = self._decorator_name(dec)
            self.references.append(
                SymbolRef(name=dec_name, location=self._loc(dec), context="decorator")
            )
            # Also visit the full decorator expression to pick up sub-references
            self.visit(dec)

        # Walk into the function body under a deeper scope
        self._scope_stack.append(name)
        self._scope_depth += 1
        self._current_scope_has_dynamic.append(False)
        for child in node.body:
            self.visit(child)
        had_dynamic = self._current_scope_has_dynamic.pop()
        self._scope_depth -= 1
        self._scope_stack.pop()

        # Stamp the definition with the dynamic flag
        if had_dynamic:
            defn.contains_dynamic_call = True

        # Default argument expressions live at the *caller* scope
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

        # Annotations
        for annotation in _iter_annotations(node):
            self.visit(annotation)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        name = node.name
        has_decorator = bool(node.decorator_list)

        # Extract base class names for framework dispatch detection
        base_names: list[str] = []
        for base in node.bases:
            try:
                base_names.append(ast.unparse(base))
            except Exception:  # noqa: BLE001
                base_names.append("<unknown>")

        defn = SymbolDef(
            name=name,
            qualified_name=self._nested_qual(name),
            kind=SymbolKind.CLASS,
            location=self._loc(node),
            is_private=name.startswith("_"),
            has_decorator=has_decorator,
            base_classes=base_names,
        )
        self.definitions.append(defn)

        # Decorators
        for dec in node.decorator_list:
            dec_name = self._decorator_name(dec)
            self.references.append(
                SymbolRef(name=dec_name, location=self._loc(dec), context="decorator")
            )
            self.visit(dec)

        # Base classes are references
        for base in node.bases:
            if isinstance(base, ast.Name):
                self.references.append(
                    SymbolRef(name=base.id, location=self._loc(base), context="base_class")
                )
            elif isinstance(base, ast.Attribute):
                root_name = _root_name(base)
                if root_name:
                    self.references.append(
                        SymbolRef(name=root_name, location=self._loc(base), context="base_class")
                    )
            self.visit(base)

        # Walk the class body under the class scope
        self._scope_stack.append(name)
        self._scope_depth += 1
        for child in node.body:
            self.visit(child)
        self._scope_depth -= 1
        self._scope_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        """Handle module/class-level assignments, including ``__all__``."""
        # Check for __all__ = [...]
        if self._scope_depth == 0:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    self.all_names = _extract_string_list(node.value)

            # Record top-level variable definitions
            for target in node.targets:
                for name_node in _extract_assigned_names(target):
                    if name_node == "__all__":
                        continue
                    defn = SymbolDef(
                        name=name_node,
                        qualified_name=self._nested_qual(name_node),
                        kind=SymbolKind.VARIABLE,
                        location=self._loc(node),
                        is_private=name_node.startswith("_"),
                    )
                    self.definitions.append(defn)

        # Visit value expression for references
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Handle annotated assignments at module/class level."""
        if self._scope_depth == 0 and isinstance(node.target, ast.Name):
            name = node.target.id
            defn = SymbolDef(
                name=name,
                qualified_name=self._nested_qual(name),
                kind=SymbolKind.VARIABLE,
                location=self._loc(node),
                is_private=name.startswith("_"),
            )
            self.definitions.append(defn)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.annotation)

    # ------------------------------------------------------------------
    # Import visitors
    # ------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            record = ImportRecord(
                module=alias.name,
                names=[],
                alias=alias.asname,
                is_wildcard=False,
                location=self._loc(node),
            )
            self.imports.append(record)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        is_wildcard = any(a.name == "*" for a in node.names)

        if is_wildcard:
            self.has_wildcard_import = True
            record = ImportRecord(
                module=module,
                names=["*"],
                alias=None,
                is_wildcard=True,
                location=self._loc(node),
            )
            self.imports.append(record)
        else:
            for alias in node.names:
                record = ImportRecord(
                    module=module,
                    names=[alias.name],
                    alias=alias.asname,
                    is_wildcard=False,
                    location=self._loc(node),
                )
                self.imports.append(record)

    # ------------------------------------------------------------------
    # Reference visitors
    # ------------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        # Only collect Load contexts (reads) — Store/Del are definitions.
        if isinstance(node.ctx, ast.Load):
            name = node.id
            is_dynamic = name in _DYNAMIC_CALL_NAMES
            context = "dynamic_call" if is_dynamic else "name"
            self.references.append(
                SymbolRef(name=name, location=self._loc(node), context=context)
            )
            # Propagate dynamic flag to innermost enclosing function scope
            if is_dynamic and self._current_scope_has_dynamic:
                self._current_scope_has_dynamic[-1] = True

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Record the root name of an attribute chain (e.g. ``utils`` in
        # ``utils.helper()``).
        root = _root_name(node)
        if root:
            self.references.append(
                SymbolRef(name=root, location=self._loc(node), context="attribute")
            )
        # Continue visiting sub-nodes to catch deeper references
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # The callee name is already captured by visit_Name / visit_Attribute.
        # Flag dynamic calls that could hide references.
        if isinstance(node.func, ast.Name) and node.func.id in _DYNAMIC_CALL_NAMES:
            # First argument to getattr/hasattr etc. might be a string ref
            if node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    self.references.append(
                        SymbolRef(
                            name=first.value,
                            location=self._loc(node),
                            context="dynamic_call",
                        )
                    )
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        """f-strings – visit inner expressions."""
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self.references.append(
                SymbolRef(name=name, location=self._loc(node), context="global")
            )

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self.references.append(
                SymbolRef(name=name, location=self._loc(node), context="nonlocal")
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _root_name(node: ast.expr) -> str | None:
    """Return the leftmost name in an attribute chain, or ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    return None


def _extract_string_list(node: ast.expr) -> list[str] | None:
    """
    Try to extract a list of string literals from an ``__all__`` assignment
    value.  Returns ``None`` if the value is not a static list/tuple of strings
    (indicating a dynamic ``__all__``).
    """
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            names.append(elt.value)
        else:
            # Non-string element → dynamic __all__, treat as unknown
            return None
    return names


def _extract_assigned_names(target: ast.expr) -> list[str]:
    """Return all simple names from a (possibly tuple-unpacked) assignment target."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_extract_assigned_names(elt))
        return names
    return []


def _iter_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    """Yield all annotation nodes from a function signature."""
    result: list[ast.expr] = []
    for arg in (
        *node.args.args,
        *node.args.posonlyargs,
        *node.args.kwonlyargs,
        *([node.args.vararg] if node.args.vararg else []),
        *([node.args.kwarg] if node.args.kwarg else []),
    ):
        if arg.annotation:
            result.append(arg.annotation)
    if node.returns:
        result.append(node.returns)
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_file(file_path: str | Path, module_name: str) -> FileIndex:
    """
    Parse *file_path* and return a populated :class:`~deadcode.models.FileIndex`.

    This function never raises.  If the file cannot be read or parsed, the
    returned ``FileIndex`` has ``parse_error`` set and empty lists elsewhere.

    Parameters
    ----------
    file_path:
        Absolute (or resolvable) path to the ``.py`` file.
    module_name:
        Dotted module name for the file (used to build qualified names).
    """
    file_path = Path(file_path)
    path_str = str(file_path)

    index = FileIndex(path=path_str, module_name=module_name)

    # Read source
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        index.parse_error = f"Cannot read file: {exc}"
        return index

    # Parse AST
    try:
        tree = ast.parse(source, filename=path_str)
    except SyntaxError as exc:
        index.parse_error = f"SyntaxError at line {exc.lineno}: {exc.msg}"
        return index
    except ValueError as exc:
        index.parse_error = f"ValueError during parse: {exc}"
        return index

    # Walk the AST
    visitor = _FileVisitor(module_name=module_name, file_path=path_str)
    visitor.visit(tree)

    index.definitions = visitor.definitions
    index.imports = visitor.imports
    index.references = visitor.references
    index.has_wildcard_import = visitor.has_wildcard_import
    index.all_names = visitor.all_names

    # Propagate __all__ membership to definitions
    if visitor.all_names is not None:
        all_set = set(visitor.all_names)
        for defn in index.definitions:
            defn.in_all = defn.name in all_set
    else:
        for defn in index.definitions:
            defn.in_all = None  # module has no __all__

    return index
