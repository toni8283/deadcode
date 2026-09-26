"""
analyzer.py – top-level orchestration and safety classification.

Responsibilities
----------------
* Orchestrate scanner → parser → indexer → graph pipeline.
* Apply the fail-closed classification rules to each definition.
* Return a structured ``AnalysisResult``.

Classification rules (fail-closed)
-----------------------------------
A definition is classified as follows:

ACTIVE
    ``DefNode.ref_count > 0`` AND no uncertainty flags are set.
    Also ACTIVE if the symbol is a well-known dunder that Python calls
    implicitly (``__init__``, ``__str__``, etc.).

REVIEW (fail-closed escalation – any of the following)
    * The definition has a decorator (framework hooks, registration, etc.)
    * The definition is listed in ``__all__``
    * ``DefNode.uncertain_due_to_wildcard`` is True
    * ``DefNode.uncertain_due_to_dynamic`` is True
    * The module has a wildcard import (``from pkg import *``)
    * The symbol name contains a dynamic pattern keyword
    * The symbol is a dunder (implicitly called by the runtime)
    * The definition is in a file with a parse error

PROVABLE
    None of the REVIEW conditions apply AND ref_count == 0.

ACTIVE (overrides PROVABLE/REVIEW when refs exist and no uncertainty)
    ref_count > 0.

Note: ACTIVE always wins over REVIEW when we have positive evidence of
use.  This means a decorated function that IS referenced will be reported
ACTIVE, not REVIEW.

Symbols excluded from candidate output
---------------------------------------
The following are silently excluded from the ``candidates`` list because
they are never meaningfully "unused":

* ``__init__.py``-only module-level statements that are intentionally
  declarative (we still index the file's symbols, but the module-level
  *imports* in ``__init__.py`` files are commonly re-exports and are
  excluded from candidacy).
* Top-level ``__all__`` variable itself.
* Import statements that are simply re-exports (covered by the in_all flag).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from deadcode.graph import DefNode, ReferenceGraph, _is_test_file, build_graph
from deadcode.indexer import build_index
from deadcode.models import (
    AnalysisResult,
    Candidate,
    EvidenceItem,
    FileIndex,
    RepoIndex,
    SafetyClassification,
    SymbolDef,
    SymbolKind,
)

# ---------------------------------------------------------------------------
# Dunder names that are implicitly used by the Python runtime / data model.
# These should always be classified REVIEW rather than PROVABLE to avoid
# false-positive deletions.
# ---------------------------------------------------------------------------
_IMPLICIT_DUNDERS: frozenset[str] = frozenset(
    {
        "__init__", "__new__", "__del__",
        "__repr__", "__str__", "__bytes__", "__format__",
        "__lt__", "__le__", "__eq__", "__ne__", "__gt__", "__ge__",
        "__hash__", "__bool__",
        "__getattr__", "__getattribute__", "__setattr__", "__delattr__",
        "__dir__",
        "__get__", "__set__", "__delete__", "__set_name__",
        "__init_subclass__", "__class_getitem__",
        "__len__", "__length_hint__",
        "__getitem__", "__setitem__", "__delitem__", "__missing__",
        "__iter__", "__reversed__", "__next__",
        "__contains__",
        "__add__", "__radd__", "__iadd__", "__sub__", "__mul__",
        "__matmul__", "__truediv__", "__floordiv__", "__mod__",
        "__divmod__", "__pow__", "__lshift__", "__rshift__",
        "__and__", "__xor__", "__or__",
        "__neg__", "__pos__", "__abs__", "__invert__",
        "__complex__", "__int__", "__float__", "__index__",
        "__round__", "__trunc__", "__floor__", "__ceil__",
        "__enter__", "__exit__",
        "__await__", "__aiter__", "__anext__", "__aenter__", "__aexit__",
        "__call__",
        "__class_getitem__",
        "__post_init__",           # dataclass hook
        "__set_name__",
        "__subclasshook__",
        # Module-level dunders that are intentionally declarative
        "__all__", "__version__", "__author__", "__email__",
        "__slots__", "__annotations__",
    }
)

# Names commonly used by frameworks as registration hooks that we cannot
# safely verify statically.
_FRAMEWORK_PATTERNS: frozenset[str] = frozenset(
    {
        "register", "route", "app", "blueprint", "signal",
        "plugin", "hook", "handler", "callback", "listener",
        "task", "job", "command", "cli",
        "main",  # entry point
        "setup",  # setuptools
        "teardown",
    }
)

# Base class names that indicate ast visitor-pattern dispatch (visit_*
# methods are invoked via ``getattr`` inside the stdlib, not explicitly).
_VISITOR_BASE_NAMES: frozenset[str] = frozenset({
    "NodeVisitor", "ast.NodeVisitor",
    "NodeTransformer", "ast.NodeTransformer",
})

# Base class names that indicate unittest discovery.
_UNITTEST_BASE_NAMES: frozenset[str] = frozenset({
    "TestCase", "unittest.TestCase",
})

# unittest lifecycle methods called implicitly by the test runner.
_UNITTEST_LIFECYCLE_METHODS: frozenset[str] = frozenset({
    "setUp", "tearDown",
    "setUpClass", "tearDownClass",
})


# ---------------------------------------------------------------------------
# Helpers for framework-aware classification
# ---------------------------------------------------------------------------


def _get_parent_class(
    defn: SymbolDef,
    definitions: dict[str, SymbolDef],
) -> SymbolDef | None:
    """Look up the enclosing class for a method, or ``None``."""
    if "." not in defn.qualified_name:
        return None
    parent_qname = defn.qualified_name.rsplit(".", 1)[0]
    parent = definitions.get(parent_qname)
    if parent is not None and parent.kind == SymbolKind.CLASS:
        return parent
    return None


def _extract_entrypoint_targets(repo_root: str) -> set[str]:
    """
    Extract qualified names referenced as entry points in ``pyproject.toml``.

    Handles ``[project.scripts]``, ``[project.gui-scripts]``, and
    ``[project.entry-points.*]`` sections per PEP 621.

    Returns an empty set if ``pyproject.toml`` is not found, unreadable, or
    contains no entry points.  Never raises.
    """
    targets: set[str] = set()
    pyproject = Path(repo_root) / "pyproject.toml"
    if not pyproject.exists():
        return targets
    try:
        import tomllib

        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except Exception:  # noqa: BLE001
        return targets

    project = data.get("project", {})
    if not isinstance(project, dict):
        return targets

    # [project.scripts] and [project.gui-scripts]
    for section in ("scripts", "gui-scripts"):
        entries = project.get(section, {})
        if isinstance(entries, dict):
            for _name, target in entries.items():
                qname = _parse_entrypoint_ref(target)
                if qname:
                    targets.add(qname)

    # [project.entry-points.<group>]
    entry_points = project.get("entry-points", {})
    if isinstance(entry_points, dict):
        for _group, entries in entry_points.items():
            if isinstance(entries, dict):
                for _name, target in entries.items():
                    qname = _parse_entrypoint_ref(target)
                    if qname:
                        targets.add(qname)

    return targets


def _parse_entrypoint_ref(target: str) -> str | None:
    """Parse ``'module.path:attribute'`` → ``'module.path.attribute'``."""
    if not isinstance(target, str) or ":" not in target:
        return None
    module, _, attr = target.partition(":")
    module = module.strip()
    attr = attr.strip()
    if not module or not attr:
        return None
    return f"{module}.{attr}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_repo(path: str | Path) -> AnalysisResult:
    """
    Run the full DeadCode analysis pipeline on the repository at *path*.

    This is the primary public entry point.

    Parameters
    ----------
    path:
        Path to the repository root to analyse.

    Returns
    -------
    AnalysisResult
        Structured, JSON-serialisable result containing all candidates with
        their classification and evidence.
    """
    repo_root = Path(path).resolve()

    # 1. Index
    index, errors = build_index(repo_root)

    # 2. Build reference graph
    graph = build_graph(index)

    # 3. Classify
    candidates = _classify_all(index, graph)

    files_with_errors = sum(1 for f in index.files if f.parse_error)

    return AnalysisResult(
        repo_root=str(repo_root),
        files_scanned=len(index.files),
        files_with_errors=files_with_errors,
        total_definitions=len(index.definitions),
        candidates=candidates,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------


def _classify_all(index: RepoIndex, graph: ReferenceGraph) -> list[Candidate]:
    """Classify every definition and return all non-ACTIVE candidates."""
    # Build a set of file paths that have parse errors so we can propagate
    # the uncertainty to their symbols.
    error_files: set[str] = {f.path for f in index.files if f.parse_error}

    # Build a map: file_path → FileIndex for quick lookup
    file_map: dict[str, FileIndex] = {f.path: f for f in index.files}

    # Pre-compute framework context
    entrypoint_qnames = _extract_entrypoint_targets(index.root)

    candidates: list[Candidate] = []

    for qname, node in graph.nodes.items():
        defn = node.definition
        file_index = file_map.get(defn.location.file)

        # Skip symbols that should never be reported
        if _should_skip(defn, file_index):
            continue

        classification, evidence, uncertainty_reasons = _classify_node(
            node, defn, file_index, error_files,
            definitions=index.definitions,
            entrypoint_qnames=entrypoint_qnames,
        )

        # ---- Structured evidence fields ----
        ref_locations = [
            {"file": r.location.file, "line": r.location.line, "context": r.context}
            for r in node.references
        ]

        # Import relationships: modules that imported this symbol's name
        import_relationships: list[str] = sorted({
            r.resolved_module
            for r in node.references
            if r.resolved_module is not None
        })

        candidates.append(
            Candidate(
                symbol=defn,
                classification=classification,
                evidence=evidence,
                ref_count=node.ref_count,
                ref_locations=ref_locations,
                test_ref_count=node.test_ref_count,
                import_relationships=import_relationships,
                is_exported=defn.in_all,
                uncertainty_reasons=uncertainty_reasons,
            )
        )

    # Stable sort: PROVABLE first, then REVIEW, then ACTIVE; within each
    # group sort by file then line number.
    _order = {
        SafetyClassification.PROVABLE: 0,
        SafetyClassification.REVIEW: 1,
        SafetyClassification.ACTIVE: 2,
    }
    candidates.sort(
        key=lambda c: (
            _order[c.classification],
            c.symbol.location.file,
            c.symbol.location.line,
        )
    )
    return candidates


def _should_skip(defn: SymbolDef, file_index: FileIndex | None) -> bool:
    """
    Return True for definitions that should never appear in the candidate list.
    """
    # Always skip __all__ itself
    if defn.name == "__all__":
        return True

    # Skip top-level imports in __init__.py files – these are commonly
    # intentional re-exports and are too noisy to report without import
    # resolution.
    if (
        file_index is not None
        and file_index.path.endswith("__init__.py")
        and defn.kind == SymbolKind.IMPORT
    ):
        return True

    return False


def _classify_node(
    node: DefNode,
    defn: SymbolDef,
    file_index: FileIndex | None,
    error_files: set[str],
    *,
    definitions: dict[str, SymbolDef] | None = None,
    entrypoint_qnames: set[str] | None = None,
) -> tuple[SafetyClassification, list[EvidenceItem], list[str]]:
    """
    Apply the fail-closed classification rules to one node.

    Returns ``(classification, evidence_list, uncertainty_reason_strings)``.
    The third element mirrors the REVIEW triggers as machine-readable strings,
    used to populate ``Candidate.uncertainty_reasons``.
    """
    evidence: list[EvidenceItem] = []
    review_reasons: list[EvidenceItem] = []

    # ------------------------------------------------------------------
    # Collect REVIEW triggers
    # ------------------------------------------------------------------

    # 1. Symbol is a dunder (implicitly called by Python runtime)
    if defn.is_dunder or defn.name in _IMPLICIT_DUNDERS:
        review_reasons.append(
            EvidenceItem(
                reason="dunder",
                detail=f"'{defn.name}' is a dunder method/attribute implicitly used by Python",
            )
        )

    # 2. Decorator present
    if defn.has_decorator:
        review_reasons.append(
            EvidenceItem(
                reason="has_decorator",
                detail="Symbol carries one or more decorators; framework registration possible",
            )
        )

    # 3. Listed in __all__ — must never be PROVABLE
    if defn.in_all:
        review_reasons.append(
            EvidenceItem(
                reason="in_all",
                detail=f"'{defn.name}' is listed in __all__ and is a public export",
            )
        )

    # 4. Wildcard import uncertainty
    if node.uncertain_due_to_wildcard:
        review_reasons.append(
            EvidenceItem(
                reason="wildcard_import",
                detail="A wildcard import (from pkg import *) may reference this symbol",
            )
        )

    # 5. Dynamic reference uncertainty (external)
    if node.uncertain_due_to_dynamic:
        review_reasons.append(
            EvidenceItem(
                reason="dynamic_reference",
                detail="A dynamic reference (getattr/eval/globals/etc.) may reference this symbol",
            )
        )

    # 5b. Symbol body contains dynamic calls (internal)
    if defn.contains_dynamic_call:
        review_reasons.append(
            EvidenceItem(
                reason="contains_dynamic_call",
                detail="This symbol's body uses dynamic access (getattr/eval/globals/importlib/etc.)",
            )
        )

    # 6. Module has wildcard import
    if file_index is not None and file_index.has_wildcard_import:
        review_reasons.append(
            EvidenceItem(
                reason="module_has_wildcard_import",
                detail="This file uses 'from pkg import *'; symbol visibility is uncertain",
            )
        )

    # 7. File had parse error
    if defn.location.file in error_files:
        review_reasons.append(
            EvidenceItem(
                reason="file_parse_error",
                detail="The source file had a syntax error; analysis may be incomplete",
            )
        )

    # 8. Name matches a framework registration pattern
    if defn.name in _FRAMEWORK_PATTERNS:
        review_reasons.append(
            EvidenceItem(
                reason="framework_pattern",
                detail=f"'{defn.name}' is a common framework entry-point name",
            )
        )

    # 9. Ambiguous import resolution (same name in multiple modules, no
    #    unambiguous import record to pin the reference)
    if node.uncertain_due_to_ambiguous_import:
        review_reasons.append(
            EvidenceItem(
                reason="ambiguous_import",
                detail=(
                    "Multiple modules define a symbol with this name and no "
                    "unambiguous import record could pin the reference"
                ),
            )
        )

    # 10. Pytest test discovery (test_* functions/methods or Test* classes in test files)
    if _is_test_file(defn.location.file):
        if defn.kind in (SymbolKind.FUNCTION, SymbolKind.ASYNC_FUNCTION) and defn.name.startswith("test_"):
            review_reasons.append(
                EvidenceItem(
                    reason="test_discovery",
                    detail=f"'{defn.name}' is a test function/method discovered implicitly by pytest",
                )
            )
        elif defn.kind == SymbolKind.CLASS and defn.name.startswith("Test"):
            review_reasons.append(
                EvidenceItem(
                    reason="test_discovery",
                    detail=f"'{defn.name}' is a test class discovered implicitly by pytest",
                )
            )

    # 11. Conftest discovery: definitions in conftest.py
    if Path(defn.location.file).name == "conftest.py":
        review_reasons.append(
            EvidenceItem(
                reason="conftest_discovery",
                detail=f"'{defn.name}' is defined in conftest.py and may be implicitly discovered by pytest",
            )
        )

    # 12. AST visitor dispatch: visit_* methods on NodeVisitor / NodeTransformer subclasses
    if defn.name.startswith("visit_"):
        parent_class = _get_parent_class(defn, definitions) if definitions else None
        if parent_class is not None and any(base in _VISITOR_BASE_NAMES for base in parent_class.base_classes):
            review_reasons.append(
                EvidenceItem(
                    reason="visitor_dispatch",
                    detail=f"'{defn.name}' is an AST visitor dispatch method on a NodeVisitor/NodeTransformer subclass",
                )
            )

    # 13. Unittest discovery: TestCase subclasses and their test_* / lifecycle methods
    parent_class = _get_parent_class(defn, definitions) if definitions else None
    if parent_class is not None and any(base in _UNITTEST_BASE_NAMES for base in parent_class.base_classes):
        if defn.name.startswith("test_") or defn.name in _UNITTEST_LIFECYCLE_METHODS:
            review_reasons.append(
                EvidenceItem(
                    reason="unittest_discovery",
                    detail=f"'{defn.name}' is a unittest test or lifecycle method on a TestCase subclass",
                )
            )
    if defn.kind == SymbolKind.CLASS and any(base in _UNITTEST_BASE_NAMES for base in defn.base_classes):
        review_reasons.append(
            EvidenceItem(
                reason="unittest_discovery",
                detail=f"'{defn.name}' inherits from TestCase and is discovered implicitly by unittest",
            )
        )

    # 14. Configuration entrypoints: targets referenced in pyproject.toml
    if entrypoint_qnames and defn.qualified_name in entrypoint_qnames:
        review_reasons.append(
            EvidenceItem(
                reason="config_entrypoint",
                detail=f"'{defn.qualified_name}' is referenced as an entry point in pyproject.toml",
            )
        )

    # ------------------------------------------------------------------
    # Determine classification
    # ------------------------------------------------------------------

    has_references = node.ref_count > 0
    has_test_refs_only = has_references and node.ref_count == node.test_ref_count

    # Symbols referenced ONLY from test files are not PROVABLE — they may
    # be the primary exercised interface of a module.  Record as REVIEW
    # (even if also referenced from non-test code, we still report ACTIVE).
    if has_test_refs_only and not review_reasons:
        review_reasons.append(
            EvidenceItem(
                reason="only_test_references",
                detail=(
                    f"Symbol has {node.test_ref_count} reference(s) but all originate "
                    "from test files; cannot prove it is unused in production code"
                ),
            )
        )

    # Build the machine-readable uncertainty reason list
    uncertainty_reasons = [e.reason for e in review_reasons]

    if has_references:
        # ACTIVE: positive evidence of use (test-only refs are still ACTIVE)
        evidence.append(
            EvidenceItem(
                reason="referenced",
                detail=(
                    f"{node.ref_count} reference(s) found"
                    + (f" ({node.test_ref_count} from tests)" if node.test_ref_count else "")
                ),
            )
        )
        evidence.extend(review_reasons)
        return SafetyClassification.ACTIVE, evidence, uncertainty_reasons

    # No references found
    evidence.append(
        EvidenceItem(
            reason="no_references",
            detail="No static references to this symbol were found",
        )
    )

    if review_reasons:
        evidence.extend(review_reasons)
        return SafetyClassification.REVIEW, evidence, uncertainty_reasons

    # Zero references, no uncertainty triggers → PROVABLE
    evidence.append(
        EvidenceItem(
            reason="provable_unused",
            detail="Symbol has no detected references and no uncertainty factors",
        )
    )
    return SafetyClassification.PROVABLE, evidence, uncertainty_reasons


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def result_to_dict(result: AnalysisResult) -> dict[str, Any]:
    """Convert an ``AnalysisResult`` to a plain JSON-serialisable dict."""
    return dataclasses.asdict(result)


def result_to_json(result: AnalysisResult, indent: int = 2) -> str:
    """Serialise an ``AnalysisResult`` to a JSON string."""
    return json.dumps(result_to_dict(result), indent=indent, default=str)
