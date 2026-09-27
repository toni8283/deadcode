"""
models.py – core data structures for DeadCode.

All dataclasses are JSON-serialisable via ``dataclasses.asdict``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SymbolKind(str, enum.Enum):
    """Broad syntactic category of a defined symbol."""

    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    CLASS = "class"
    VARIABLE = "variable"
    IMPORT = "import"  # top-level import statement treated as a symbol
    MODULE = "module"


class SafetyClassification(str, enum.Enum):
    """
    Result of attempting to classify a symbol as unused.

    PROVABLE
        Strong static evidence indicates the symbol is unused.  All known
        reference sites have been checked and none were found.  No dynamic
        patterns that could hide a reference were detected.

    REVIEW
        Static analysis cannot safely determine whether the symbol is
        unused.  At least one of the following uncertain conditions was
        detected: dynamic import, decorator, ``__all__`` membership,
        reflection / ``getattr`` usage, wildcard import, string-based
        reference, or framework-registration pattern.

    ACTIVE
        One or more static references to this symbol were found.
    """

    PROVABLE = "PROVABLE"
    REVIEW = "REVIEW"
    ACTIVE = "ACTIVE"


# ---------------------------------------------------------------------------
# Source-location helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """File + line where a symbol is defined or referenced."""

    file: str          # str path (JSON-friendly)
    line: int


# ---------------------------------------------------------------------------
# Symbol definitions
# ---------------------------------------------------------------------------


@dataclass
class SymbolDef:
    """
    A symbol that was *defined* somewhere in the repository.

    Attributes
    ----------
    name:
        Simple (un-qualified) name of the symbol.
    qualified_name:
        Dot-separated fully qualified name relative to the repo root,
        e.g. ``mypackage.utils.helper_fn``.
    kind:
        Syntactic category (function, class, variable …).
    location:
        File path and line number of the definition.
    is_private:
        ``True`` when the name starts with ``_`` (single or double underscore).
    in_all:
        ``True`` when the name appears in the module's ``__all__`` list.
        ``None`` means no ``__all__`` was found in the module.
    has_decorator:
        ``True`` when the function/class carries one or more decorators.
    is_dunder:
        ``True`` for names like ``__init__``, ``__str__``, etc.
    """

    name: str
    qualified_name: str
    kind: SymbolKind
    location: Location
    is_private: bool = False
    in_all: bool | None = None  # None = module has no __all__
    has_decorator: bool = False
    is_dunder: bool = False
    base_classes: list[str] = field(default_factory=list)

    # True when the function/class body contains a dynamic call (getattr,
    # eval, globals, importlib, etc.).
    contains_dynamic_call: bool = False

    # Extra evidence stored for the CLI to surface later
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Import records
# ---------------------------------------------------------------------------


@dataclass
class ImportRecord:
    """
    A single ``import`` or ``from … import`` statement found in a file.

    Attributes
    ----------
    module:
        The module being imported (e.g. ``os.path``).
    names:
        Names imported from the module.  Empty list for bare ``import pkg``
        statements.
    alias:
        The local alias if ``as`` was used, otherwise ``None``.
    is_wildcard:
        ``True`` for ``from pkg import *``.
    location:
        Where the import statement appears.
    """

    module: str
    names: list[str]
    alias: str | None
    is_wildcard: bool
    location: Location
    level: int = 0


# ---------------------------------------------------------------------------
# Reference records
# ---------------------------------------------------------------------------


@dataclass
class SymbolRef:
    """
    A place where a name is *used* (read / called / passed / inherited …).

    Attributes
    ----------
    name:
        The bare name as it appears in source (may be an attribute chain
        prefix, e.g. ``utils`` in ``utils.helper()``).
    location:
        Where the usage appears.
    context:
        Descriptive label for the kind of usage, e.g. ``"call"``,
        ``"attribute"``, ``"base_class"``, ``"decorator"``.
    is_from_test:
        ``True`` when this reference originates from a test file
        (``test_*.py``, ``*_test.py``, or inside a ``tests/`` directory).
    resolved_module:
        When import-aware resolution pinned this reference to a specific
        module (e.g. ``utils``), that module name is stored here.
        ``None`` means the resolution fell back to bare-name matching.
    """

    name: str
    location: Location
    context: str = "name"
    is_from_test: bool = False
    resolved_module: str | None = None


# ---------------------------------------------------------------------------
# Per-file parse result
# ---------------------------------------------------------------------------


@dataclass
class FileIndex:
    """Everything extracted from a single Python source file."""

    path: str                                   # absolute string path
    module_name: str                            # dotted module name relative to repo root
    definitions: list[SymbolDef] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    references: list[SymbolRef] = field(default_factory=list)
    imported_modules: set[str] = field(default_factory=set)
    has_wildcard_import: bool = False
    has_main_block: bool = False
    has_shebang: bool = False
    has_unresolved_dynamic_import: bool = False
    dynamic_import_targets: set[str] = field(default_factory=set)
    all_names: list[str] | None = None          # contents of __all__, or None
    parse_error: str | None = None              # error message if AST parse failed


# ---------------------------------------------------------------------------
# Whole-repo index
# ---------------------------------------------------------------------------


@dataclass
class RepoIndex:
    """Aggregated index for an entire repository."""

    root: str                                   # absolute repo root path
    files: list[FileIndex] = field(default_factory=list)

    # Convenience maps built by the Indexer
    # qualified_name -> SymbolDef
    definitions: dict[str, SymbolDef] = field(default_factory=dict)
    # bare name -> list[SymbolDef]  (multiple defs with same short name)
    name_to_defs: dict[str, list[SymbolDef]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Candidate / result types
# ---------------------------------------------------------------------------


@dataclass
class EvidenceItem:
    """A single piece of evidence explaining a classification decision."""

    reason: str
    detail: str = ""


@dataclass
class Candidate:
    """
    A symbol flagged as potentially unused, together with the evidence and
    final classification.

    Structured evidence fields
    --------------------------
    These mirror the prose ``evidence`` list but in machine-readable form,
    sufficient for a CLI to render a rich explanation without re-parsing.

    ref_count:
        Total number of static references found (0 for PROVABLE/REVIEW).
    ref_locations:
        List of (file, line) pairs for every reference site found.
    test_ref_count:
        Number of references that originate from test files.
    import_relationships:
        For each ``from module import name`` import that brought this
        symbol's name into a consumer file, the importing module name
        is recorded here.
    is_exported:
        ``True`` if the symbol is listed in its module's ``__all__``.
        ``None`` if the module has no ``__all__``.
    uncertainty_reasons:
        Machine-readable list of REVIEW-trigger reason strings, e.g.
        ``["wildcard_import", "has_decorator"]``.  Empty for PROVABLE/ACTIVE.
    """

    symbol: SymbolDef
    classification: SafetyClassification
    evidence: list[EvidenceItem] = field(default_factory=list)

    # Structured evidence (machine-readable, JSON-serialisable)
    ref_count: int = 0
    ref_locations: list[dict[str, Any]] = field(default_factory=list)
    test_ref_count: int = 0
    import_relationships: list[str] = field(default_factory=list)
    is_exported: bool | None = None   # mirrors symbol.in_all
    uncertainty_reasons: list[str] = field(default_factory=list)

    # Derived convenience properties
    @property
    def qualified_name(self) -> str:
        return self.symbol.qualified_name

    @property
    def file(self) -> str:
        return self.symbol.location.file

    @property
    def line(self) -> int:
        return self.symbol.location.line


@dataclass
class AnalysisResult:
    """
    Top-level result returned by ``analyze_repo()``.

    JSON-serialisable via ``dataclasses.asdict(result)``.
    """

    repo_root: str
    files_scanned: int
    files_with_errors: int
    total_definitions: int
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def provable(self) -> list[Candidate]:
        return [c for c in self.candidates if c.classification == SafetyClassification.PROVABLE]

    @property
    def review(self) -> list[Candidate]:
        return [c for c in self.candidates if c.classification == SafetyClassification.REVIEW]

    @property
    def active(self) -> list[Candidate]:
        return [c for c in self.candidates if c.classification == SafetyClassification.ACTIVE]
