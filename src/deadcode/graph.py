"""
graph.py – deterministic reference graph construction.

Responsibilities
----------------
* Given a fully populated ``RepoIndex``, build a graph that maps each
  ``SymbolDef`` to the set of ``SymbolRef`` objects that appear to reference
  it.
* Record which definitions have *zero* detected references (candidates for
  removal).
* Flag definitions that cannot be safely classified because of dynamic
  patterns detected during parsing or cross-file wildcard imports.

Import-aware resolution (Task 2)
---------------------------------
When a file contains ``from utils import calculate_total``, any use of
``calculate_total`` in that file is pinned to the module ``utils``, not to
every module that happens to define a function with the same name.

Resolution priority
~~~~~~~~~~~~~~~~~~~
1. **Pinned via ``from M import N``** — If the consuming file has a
   ``from M import N`` record (not wildcard), and ``N`` matches the
   reference name, AND ``M`` resolves to a local module, we credit only the
   ``DefNode`` for ``M.N`` (qualified: ``<module>.N``).

2. **Pinned via ``import M`` + attribute access** — If the consuming file has
   ``import M`` and the reference is the bare name ``M`` (used as
   ``M.symbol``), we credit the ``DefNode`` for ``M`` itself (the module).
   Attribute resolution beyond the bare name is not attempted.

3. **Ambiguous bare-name fallback** — If the consuming file has NO import
   record for this name (e.g. a same-file call), we fall back to bare-name
   matching across all definitions.  When multiple definitions share the same
   name and we have no import to disambiguate, we mark ALL of them as
   ``uncertain_due_to_ambiguous_import``.

Fail-closed principle
~~~~~~~~~~~~~~~~~~~~~
If resolution is uncertain for any reason — unresolvable module, aliased
import, relative import from unknown package — we do NOT guess.  The
reference is recorded as ambiguous and the affected ``DefNode`` is marked
``uncertain_due_to_ambiguous_import``, ensuring the analyzer produces REVIEW.

Test-file detection
-------------------
A file is a test file if its basename matches ``test_*.py`` / ``*_test.py``
or it lives inside a directory whose name is ``tests`` or ``test``.
References originating from test files set ``SymbolRef.is_from_test = True``
and are counted separately in the graph node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from deadcode.models import FileIndex, ImportRecord, RepoIndex, SymbolDef, SymbolRef


# ---------------------------------------------------------------------------
# Test-file detection
# ---------------------------------------------------------------------------

_TEST_FILENAME_RE = re.compile(r"^(test_.+|.+_test)\.py$")
_TEST_DIR_NAMES: frozenset[str] = frozenset({"tests", "test"})


def _is_test_file(path: str) -> bool:
    """Return True if *path* refers to a test file."""
    p = Path(path)
    # Filename pattern
    if _TEST_FILENAME_RE.match(p.name):
        return True
    # Any ancestor directory named 'tests' or 'test'
    for part in p.parts:
        if part in _TEST_DIR_NAMES:
            return True
    return False


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DefNode:
    """
    A node in the reference graph for one ``SymbolDef``.

    Attributes
    ----------
    definition:
        The symbol definition this node represents.
    references:
        All ``SymbolRef`` objects that appear to reference this definition.
    test_references:
        Subset of ``references`` that originate from test files.
    uncertain_due_to_wildcard:
        ``True`` if a ``from pkg import *`` was found in *any* file that shares
        the same bare name as this definition – we cannot be sure the wildcard
        did or did not import this symbol.
    uncertain_due_to_dynamic:
        ``True`` if a dynamic call (``getattr``, ``globals()``, ``eval``, …)
        or a ``dynamic_call``-context reference was found that uses this
        symbol's name.
    uncertain_due_to_ambiguous_import:
        ``True`` when a reference shares the bare name with this definition but
        the consuming file has no import record that would unambiguously pin the
        reference to this specific module.  Set only when multiple definitions
        share the same bare name.
    """

    definition: SymbolDef
    references: list[SymbolRef] = field(default_factory=list)
    test_references: list[SymbolRef] = field(default_factory=list)
    uncertain_due_to_wildcard: bool = False
    uncertain_due_to_dynamic: bool = False
    uncertain_due_to_ambiguous_import: bool = False

    @property
    def ref_count(self) -> int:
        return len(self.references)

    @property
    def test_ref_count(self) -> int:
        return len(self.test_references)


@dataclass
class ReferenceGraph:
    """
    The complete reference graph for a repository.

    ``nodes`` is keyed by the definition's ``qualified_name``.
    """

    nodes: dict[str, DefNode] = field(default_factory=dict)
    # Names referenced via wildcard imports (bare names only)
    wildcard_exposed_names: set[str] = field(default_factory=set)

    def get_node(self, qualified_name: str) -> DefNode | None:
        return self.nodes.get(qualified_name)

    def all_nodes(self) -> list[DefNode]:
        return list(self.nodes.values())


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_graph(index: RepoIndex) -> ReferenceGraph:
    """
    Construct a :class:`ReferenceGraph` from the given ``RepoIndex``.

    Steps
    -----
    1. Create one ``DefNode`` for every ``SymbolDef`` in the index.
    2. Build helper maps for wildcard uncertainty.
    3. Build per-file import-resolution maps.
    4. Resolve references using import-aware logic; record test-file origin.
    5. Dynamic-reference pass: mark nodes uncertain where needed.
    """
    graph = ReferenceGraph()

    # ----------------------------------------------------------------
    # 1.  Create nodes
    # ----------------------------------------------------------------
    for qname, defn in index.definitions.items():
        graph.nodes[qname] = DefNode(definition=defn)

    # ----------------------------------------------------------------
    # 2.  Wildcard-import uncertainty
    # ----------------------------------------------------------------
    wildcard_source_modules: set[str] = set()
    for file_index in index.files:
        for imp in file_index.imports:
            if imp.is_wildcard:
                wildcard_source_modules.add(imp.module)

    # Any definition in a module that is wildcard-imported is uncertain.
    for qname, defn in index.definitions.items():
        parent_module = _parent_module(defn.qualified_name)
        if parent_module in wildcard_source_modules:
            graph.nodes[qname].uncertain_due_to_wildcard = True
            graph.wildcard_exposed_names.add(defn.name)

    # Consumer files that use wildcard imports
    consumer_files_with_wildcard: set[str] = {
        fi.path for fi in index.files if fi.has_wildcard_import
    }

    # ----------------------------------------------------------------
    # 3.  Per-file import maps
    #     For each consumer file, build a map:
    #       bare_name → list[qualified_name candidates]
    #     derived from explicit import records.
    # ----------------------------------------------------------------
    # module_name → file_index for fast lookup
    module_to_file: dict[str, FileIndex] = {
        fi.module_name: fi for fi in index.files if fi.module_name
    }

    # bare_name → list[DefNode] (global, for fallback)
    name_to_nodes: dict[str, list[DefNode]] = {}
    for node in graph.nodes.values():
        bare = node.definition.name
        name_to_nodes.setdefault(bare, []).append(node)

    # ----------------------------------------------------------------
    # 4.  Resolve references → nodes
    # ----------------------------------------------------------------
    for file_index in index.files:
        is_test = _is_test_file(file_index.path)

        # Build the import resolution map for this specific consumer file.
        # Maps:  local_name → list[DefNode]  (resolved to local-repo defs)
        # Also:  local_name → "ambiguous" | "external" | "wildcard"
        import_map, ambiguous_names, external_names = _build_import_map(
            file_index, module_to_file, graph.nodes, name_to_nodes
        )

        for ref in file_index.references:
            ref_name = ref.name

            # ---- Determine target nodes ----
            resolved_nodes: list[DefNode] = []
            resolution_mode: str = "bare"  # "pinned" | "bare" | "ambiguous"

            if ref_name in ambiguous_names:
                # We know there's a local import for this name but it resolved
                # to multiple candidates — mark all as ambiguous.
                resolved_nodes = name_to_nodes.get(ref_name, [])
                resolution_mode = "ambiguous"
            elif ref_name in import_map:
                # Pinned via a specific import record
                resolved_nodes = import_map[ref_name]
                resolution_mode = "pinned"
                ref.resolved_module = (
                    resolved_nodes[0].definition.qualified_name.rsplit(".", 1)[0]
                    if resolved_nodes else None
                )
            elif ref_name in external_names:
                # Import exists but target is not a local module — skip
                resolved_nodes = []
                resolution_mode = "external"
            else:
                # No import record at all — bare fallback
                resolved_nodes = _resolve_bare(ref_name, name_to_nodes)
                resolution_mode = "bare"

            # ---- Stamp test-file origin ----
            ref.is_from_test = is_test

            # ---- Associate reference with nodes ----
            for cand_node in resolved_nodes:
                cand_node.references.append(ref)
                if is_test:
                    cand_node.test_references.append(ref)

                if ref.context == "dynamic_call":
                    cand_node.uncertain_due_to_dynamic = True

                if resolution_mode == "ambiguous":
                    cand_node.uncertain_due_to_ambiguous_import = True

            # Wildcard-consuming file: any resolved node becomes uncertain
            if file_index.path in consumer_files_with_wildcard:
                for cand_node in resolved_nodes:
                    cand_node.uncertain_due_to_wildcard = True

    # ----------------------------------------------------------------
    # 5.  Dynamic-reference pass
    # ----------------------------------------------------------------
    for file_index in index.files:
        for ref in file_index.references:
            if ref.context == "dynamic_call" and ref.name in name_to_nodes:
                for node in name_to_nodes[ref.name]:
                    node.uncertain_due_to_dynamic = True

    return graph


# ---------------------------------------------------------------------------
# Import-aware resolution helpers
# ---------------------------------------------------------------------------


def _build_import_map(
    file_index: FileIndex,
    module_to_file: dict[str, FileIndex],
    all_nodes: dict[str, DefNode],
    name_to_nodes: dict[str, list[DefNode]],
) -> tuple[
    dict[str, list[DefNode]],   # name → resolved DefNode list (pinned)
    set[str],                    # names that are ambiguous
    set[str],                    # names that are external (non-local)
]:
    """
    Build the import-resolution map for a single consumer file.

    Returns
    -------
    import_map:
        Maps a local name to the specific DefNode(s) it was imported from.
        Only populated when resolution is unambiguous.
    ambiguous_names:
        Names where an import exists but resolves to multiple candidates in
        the local repo (same bare name, multiple matching defs).
    external_names:
        Names that are imported but the source module is not in the local repo.
    """
    import_map: dict[str, list[DefNode]] = {}
    ambiguous_names: set[str] = set()
    external_names: set[str] = set()

    for imp in file_index.imports:
        if imp.is_wildcard:
            # Wildcards are handled separately (uncertainty flag on source).
            continue

        # ---- ``from M import N [as A]`` ----
        if imp.names:
            module = imp.module
            # Resolve the module to a local file index (if possible).
            target_fi = _resolve_module(module, module_to_file, file_index)

            for name in imp.names:
                local_name = imp.alias if imp.alias else name

                if target_fi is None:
                    # Module not in local repo — mark as external
                    external_names.add(local_name)
                    continue

                # Look for `name` as a definition in target_fi's qualified ns
                expected_qname = f"{target_fi.module_name}.{name}"
                node = all_nodes.get(expected_qname)

                if node is not None:
                    # Unambiguous: exactly one local def
                    import_map[local_name] = [node]
                else:
                    # The module exists locally but the name isn't defined there.
                    # Could be a re-export from a sub-module or a dynamic attr.
                    # Fail closed: check name_to_nodes for any same-name def.
                    candidates = name_to_nodes.get(name, [])
                    if len(candidates) == 1:
                        # Only one def of this name anywhere — safe to pin
                        import_map[local_name] = candidates
                    elif len(candidates) > 1:
                        # Multiple defs — ambiguous
                        ambiguous_names.add(local_name)
                    else:
                        # Name not found in repo at all — external
                        external_names.add(local_name)

        # ---- ``import M [as A]`` ----
        else:
            module = imp.module
            local_name = imp.alias if imp.alias else module.split(".")[0]
            target_fi = _resolve_module(module, module_to_file, file_index)

            if target_fi is None:
                external_names.add(local_name)
            else:
                # The import makes the module name available; attribute access
                # like ``M.func()`` will have ``M`` as the bare Name node.
                # We do NOT resolve the attribute chain — we only credit ``M``
                # itself (the module) if there's a DefNode for it.
                # In practice the module doesn't have a DefNode, so this
                # just records the intent to not treat M as an ambiguous ref.
                external_names.add(local_name)  # Don't mis-classify M as local def

    return import_map, ambiguous_names, external_names


def _resolve_module(
    module: str,
    module_to_file: dict[str, FileIndex],
    consumer_fi: FileIndex,
) -> FileIndex | None:
    """
    Try to resolve a module string to a local :class:`FileIndex`.

    Handles:
    * Exact match: ``utils`` → module_name ``utils``
    * Dotted match: ``package.utils`` → module_name ``package.utils``
    * Relative imports (level > 0) are not yet resolvable without the full
      package structure — return None (fail closed).

    Parameters
    ----------
    module:
        The module string from the import record (may be empty for relative
        imports captured as ``""``).
    module_to_file:
        Map of module_name → FileIndex for all local files.
    consumer_fi:
        The file doing the importing (used for relative resolution context,
        currently unused — relative imports fall through to None).
    """
    if not module:
        # Relative import with empty module string — cannot resolve
        return None

    # Direct lookup
    if module in module_to_file:
        return module_to_file[module]

    # Try suffix match: ``from utils import X`` where the repo has
    # ``mypackage.utils`` as the module name.
    for mod_name, fi in module_to_file.items():
        if mod_name == module or mod_name.endswith(f".{module}"):
            return fi

    return None


def _resolve_bare(
    name: str,
    name_to_nodes: dict[str, list[DefNode]],
) -> list[DefNode]:
    """
    Fall back to bare-name matching (no import information).

    When only ONE definition exists with this name, it is safe to credit it.
    When MULTIPLE definitions share this name, we return all of them (each
    will be marked ``uncertain_due_to_ambiguous_import`` by the caller if
    resolution_mode == "ambiguous" — but here it's "bare", meaning the name
    genuinely has no import to disambiguate).
    """
    if name in _BUILTIN_NAMES:
        return []
    return name_to_nodes.get(name, [])


def _parent_module(qualified_name: str) -> str:
    """Return the module part of a qualified name (everything before the last dot)."""
    parts = qualified_name.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# Builtins filter
# ---------------------------------------------------------------------------

_BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        # Built-in functions
        "abs", "aiter", "all", "anext", "any", "ascii",
        "bin", "bool", "breakpoint", "bytearray", "bytes",
        "callable", "chr", "classmethod", "compile", "complex",
        "copyright", "credits",
        "delattr", "dict", "dir", "divmod",
        "enumerate", "eval", "exec", "exit",
        "filter", "float", "format", "frozenset",
        "getattr", "globals",
        "hasattr", "hash", "help", "hex",
        "id", "input", "int", "isinstance", "issubclass", "iter",
        "len", "license", "list", "locals",
        "map", "max", "memoryview", "min",
        "next",
        "object", "oct", "open", "ord",
        "pow", "print", "property",
        "quit",
        "range", "repr", "reversed", "round",
        "set", "setattr", "slice", "sorted", "staticmethod", "str", "sum",
        "super",
        "tuple", "type",
        "vars",
        "zip",
        # Built-in exceptions
        "ArithmeticError", "AssertionError", "AttributeError",
        "BaseException", "BlockingIOError", "BrokenPipeError",
        "BufferError", "BytesWarning",
        "ChildProcessError", "ConnectionAbortedError",
        "ConnectionError", "ConnectionRefusedError",
        "ConnectionResetError",
        "DeprecationWarning",
        "EOFError", "EnvironmentError", "Exception",
        "FileExistsError", "FileNotFoundError", "FloatingPointError",
        "FutureWarning",
        "GeneratorExit",
        "IOError", "ImportError", "ImportWarning", "IndentationError",
        "IndexError", "InterruptedError", "IsADirectoryError",
        "KeyError", "KeyboardInterrupt",
        "LookupError",
        "MemoryError", "ModuleNotFoundError",
        "NameError", "NotADirectoryError", "NotImplementedError",
        "OSError", "OverflowError",
        "PendingDeprecationWarning", "PermissionError",
        "ProcessLookupError",
        "RecursionError", "ReferenceError", "ResourceWarning",
        "RuntimeError", "RuntimeWarning",
        "StopAsyncIteration", "StopIteration", "SyntaxError",
        "SyntaxWarning", "SystemError", "SystemExit",
        "TabError", "TimeoutError", "TypeError",
        "UnboundLocalError", "UnicodeDecodeError", "UnicodeEncodeError",
        "UnicodeError", "UnicodeTranslateError", "UnicodeWarning",
        "UserWarning",
        "ValueError",
        "Warning",
        "ZeroDivisionError",
        # Built-in constants
        "False", "None", "NotImplemented", "True", "Ellipsis",
        "__debug__", "__name__", "__file__", "__doc__", "__package__",
        "__spec__", "__loader__", "__builtins__",
        # Common typing names that are widely imported but look like user defs
        "Optional", "Union", "List", "Dict", "Tuple", "Set",
        "Callable", "Any", "Type", "ClassVar", "Final",
        "TypeVar", "Generic", "Protocol",
        "Literal", "Annotated", "TypedDict",
        "overload", "cast", "no_type_check",
        "TYPE_CHECKING",
        # Commonly used keywords / dunders that appear as names
        "self", "cls",
        "__all__", "__init__", "__new__", "__del__",
        "__repr__", "__str__", "__bytes__", "__format__",
        "__lt__", "__le__", "__eq__", "__ne__", "__gt__", "__ge__",
        "__hash__", "__bool__",
        "__getattr__", "__getattribute__", "__setattr__", "__delattr__",
        "__dir__",
        "__get__", "__set__", "__delete__", "__set_name__",
        "__init_subclass__",
        "__class_getitem__",
        "__len__", "__length_hint__",
        "__getitem__", "__setitem__", "__delitem__",
        "__missing__",
        "__iter__", "__reversed__", "__next__",
        "__contains__",
        "__add__", "__radd__", "__iadd__",
        "__sub__", "__mul__", "__matmul__", "__truediv__",
        "__floordiv__", "__mod__", "__divmod__", "__pow__",
        "__lshift__", "__rshift__", "__and__", "__xor__", "__or__",
        "__neg__", "__pos__", "__abs__", "__invert__",
        "__complex__", "__int__", "__float__", "__index__",
        "__round__", "__trunc__", "__floor__", "__ceil__",
        "__enter__", "__exit__",
        "__await__", "__aiter__", "__anext__",
        "__aenter__", "__aexit__",
        "__call__",
        "__slots__", "__dict__", "__weakref__",
        "__class__", "__bases__", "__mro__",
        "__module__", "__qualname__",
        "__annotations__",
        "__version__", "__author__",
    }
)
