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

Graph model
-----------
The graph is intentionally *conservative*:

  Definition  ──referenced_by──▶  list[SymbolRef]

A reference is attributed to a definition when:
  1. The reference's bare name matches the definition's simple ``name``, OR
  2. The reference's bare name matches the last segment of the definition's
     ``qualified_name`` (e.g. ``helper`` matches ``mypackage.utils.helper``).

We do NOT attempt to resolve fully-qualified dotted names beyond step 2 above
because Python's dynamic import system means we could never be certain.  If we
cannot resolve a reference, it is silently dropped (the definition remains
unreferenced, which is the safe direction).

Wildcard imports (``from pkg import *``) prevent us from knowing what names
were introduced into a module's namespace.  Any definition whose simple name
*could* have been imported via a wildcard import is conservatively flagged as
``uncertain_due_to_wildcard``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from deadcode.models import FileIndex, RepoIndex, SymbolDef, SymbolRef


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
    uncertain_due_to_wildcard:
        ``True`` if a ``from pkg import *`` was found in *any* file that shares
        the same bare name as this definition – we cannot be sure the wildcard
        did or did not import this symbol.
    uncertain_due_to_dynamic:
        ``True`` if a dynamic call (``getattr``, ``globals()``, ``eval``, …)
        or a ``dynamic_call``-context reference was found that uses this
        symbol's name.
    """

    definition: SymbolDef
    references: list[SymbolRef] = field(default_factory=list)
    uncertain_due_to_wildcard: bool = False
    uncertain_due_to_dynamic: bool = False

    @property
    def ref_count(self) -> int:
        return len(self.references)


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
    2. Collect all ``SymbolRef`` objects from every file.
    3. For each reference, attempt to resolve it to one or more ``DefNode``
       objects and record the association.
    4. Mark nodes that are affected by wildcard imports or dynamic references.
    """
    graph = ReferenceGraph()

    # ----------------------------------------------------------------
    # 1.  Create nodes
    # ----------------------------------------------------------------
    for qname, defn in index.definitions.items():
        graph.nodes[qname] = DefNode(definition=defn)

    # ----------------------------------------------------------------
    # 2.  Collect wildcard-exposed names
    #
    #     Any file that has ``from pkg import *`` introduces *unknown* names
    #     into its namespace.  We record the bare names of all definitions in
    #     the *source* package so we can flag them as uncertain.
    # ----------------------------------------------------------------
    wildcard_source_modules: set[str] = set()
    for file_index in index.files:
        for imp in file_index.imports:
            if imp.is_wildcard:
                wildcard_source_modules.add(imp.module)

    # Anything that *could* be exported from a wildcard-imported module is
    # uncertain.  Since we may not have the source of the imported module, we
    # conservatively mark every definition whose module matches.
    for qname, defn in index.definitions.items():
        module = defn.location.file  # used for file-level check below
        if defn.qualified_name.rsplit(".", 1)[0] in wildcard_source_modules:
            graph.nodes[qname].uncertain_due_to_wildcard = True
            graph.wildcard_exposed_names.add(defn.name)

    # Additionally flag any *consumer* file that does ``from x import *``:
    # names used in that file might resolve to things we can't know.
    consumer_files_with_wildcard: set[str] = set()
    for file_index in index.files:
        if file_index.has_wildcard_import:
            consumer_files_with_wildcard.add(file_index.path)

    # ----------------------------------------------------------------
    # 3.  Resolve references → nodes
    # ----------------------------------------------------------------
    # Build a reverse map: bare_name → list[DefNode] for fast lookup
    name_to_nodes: dict[str, list[DefNode]] = {}
    for node in graph.nodes.values():
        bare = node.definition.name
        name_to_nodes.setdefault(bare, []).append(node)

    for file_index in index.files:
        for ref in file_index.references:
            # Resolve the reference to candidate definition nodes
            candidates = _resolve_ref(ref.name, name_to_nodes, index)

            for cand_node in candidates:
                cand_node.references.append(ref)

                # Dynamic call context → mark the target as uncertain
                if ref.context == "dynamic_call":
                    cand_node.uncertain_due_to_dynamic = True

            # Any reference made from a wildcard-importing file is inherently
            # uncertain for the *referenced* symbol (we can't tell if it came
            # via the wildcard or a direct def).
            if file_index.path in consumer_files_with_wildcard:
                for cand_node in candidates:
                    cand_node.uncertain_due_to_wildcard = True

    # ----------------------------------------------------------------
    # 4.  Dynamic-reference pass: if a name appears in a getattr/eval/exec
    #     call anywhere in the repo, mark it uncertain.
    # ----------------------------------------------------------------
    for file_index in index.files:
        for ref in file_index.references:
            if ref.context == "dynamic_call" and ref.name in name_to_nodes:
                for node in name_to_nodes[ref.name]:
                    node.uncertain_due_to_dynamic = True

    return graph


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------


def _resolve_ref(
    name: str,
    name_to_nodes: dict[str, list[DefNode]],
    index: RepoIndex,
) -> list[DefNode]:
    """
    Map a bare reference name to zero or more definition nodes.

    Strategy
    --------
    * Exact bare-name match in ``name_to_nodes``.
    * We intentionally do NOT attempt to resolve dotted attribute chains
      beyond their root component – that is already split out by the parser.
    """
    # Skip Python builtins and common keywords that will never match a
    # user-defined symbol.  This is a best-effort optimisation, not a
    # correctness requirement.
    if name in _BUILTIN_NAMES:
        return []

    return name_to_nodes.get(name, [])


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
