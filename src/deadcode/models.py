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
    """

    name: str
    location: Location
    context: str = "name"


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
    has_wildcard_import: bool = False
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
    """

    symbol: SymbolDef
    classification: SafetyClassification
    evidence: list[EvidenceItem] = field(default_factory=list)

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
