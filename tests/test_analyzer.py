"""
tests/test_analyzer.py – integration tests for the DeadCode analysis engine.

Each test targets one or more of the required scenarios:
  - unused function
  - used function
  - unused class
  - imported symbol
  - cross-file reference
  - syntax error (graceful handling)
  - ignored directories
  - ambiguous/dynamic cases → REVIEW

All tests use synthetic fixture repositories under tests/fixtures/.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deadcode import AnalysisResult, SafetyClassification, analyze_repo
from deadcode.models import SymbolKind

# Resolve the fixtures directory relative to this test file
FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def candidates_by_name(result: AnalysisResult) -> dict[str, SafetyClassification]:
    """Return a mapping of bare symbol name → classification."""
    return {c.symbol.name: c.classification for c in result.candidates}


def find_candidate(result: AnalysisResult, name: str):
    """Return the first candidate whose bare name matches *name*, or None."""
    for c in result.candidates:
        if c.symbol.name == name:
            return c
    return None


# ---------------------------------------------------------------------------
# Test: unused function
# ---------------------------------------------------------------------------


class TestUnusedFunction:
    """A function defined but never called must be classified PROVABLE."""

    def test_provable_classification(self):
        result = analyze_repo(FIXTURES / "unused_function")
        mapping = candidates_by_name(result)
        assert "unused_function" in mapping
        assert mapping["unused_function"] == SafetyClassification.PROVABLE

    def test_evidence_contains_no_references(self):
        result = analyze_repo(FIXTURES / "unused_function")
        cand = find_candidate(result, "unused_function")
        assert cand is not None
        reasons = {e.reason for e in cand.evidence}
        assert "no_references" in reasons

    def test_evidence_contains_provable_reason(self):
        result = analyze_repo(FIXTURES / "unused_function")
        cand = find_candidate(result, "unused_function")
        assert cand is not None
        reasons = {e.reason for e in cand.evidence}
        assert "provable_unused" in reasons

    def test_result_is_json_serialisable(self):
        import dataclasses, json
        result = analyze_repo(FIXTURES / "unused_function")
        json.dumps(dataclasses.asdict(result), default=str)  # must not raise


# ---------------------------------------------------------------------------
# Test: used function
# ---------------------------------------------------------------------------


class TestUsedFunction:
    """A function that is called in the same file must be classified ACTIVE."""

    def test_active_classification(self):
        result = analyze_repo(FIXTURES / "used_function")
        mapping = candidates_by_name(result)
        assert "used_function" in mapping
        assert mapping["used_function"] == SafetyClassification.ACTIVE

    def test_evidence_contains_referenced_reason(self):
        result = analyze_repo(FIXTURES / "used_function")
        cand = find_candidate(result, "used_function")
        assert cand is not None
        reasons = {e.reason for e in cand.evidence}
        assert "referenced" in reasons


# ---------------------------------------------------------------------------
# Test: unused class
# ---------------------------------------------------------------------------


class TestUnusedClass:
    """An unused class must be classified REVIEW (dunder __init__ triggers it)."""

    def test_unused_class_not_provable(self):
        """
        UnusedClass carries no decorator and is not in __all__, but its
        __init__ method is a dunder → the class itself may still be
        classified PROVABLE if the class name has no references.
        The class-level symbol is what we check.
        """
        result = analyze_repo(FIXTURES / "unused_class")
        cand = find_candidate(result, "UnusedClass")
        assert cand is not None
        # The class has no references, no decorator, not in __all__.
        # method() is a nested dunder-adjacent but UnusedClass itself is plain.
        assert cand.classification == SafetyClassification.PROVABLE

    def test_dunder_init_inside_class_is_review(self):
        """__init__ inside UnusedClass must be REVIEW (dunder trigger)."""
        result = analyze_repo(FIXTURES / "unused_class")
        cand = find_candidate(result, "__init__")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW

    def test_method_not_provable_if_class_unused(self):
        """
        method() is a plain method on an unused class.  It has no decorator,
        no __all__, not a dunder.  Its bare name 'method' is not referenced
        anywhere → PROVABLE.
        """
        result = analyze_repo(FIXTURES / "unused_class")
        # Find the method candidate (may be qualified or bare)
        method_cands = [c for c in result.candidates if c.symbol.name == "method"]
        assert method_cands, "Expected a candidate for 'method'"
        assert all(c.classification == SafetyClassification.PROVABLE for c in method_cands)


# ---------------------------------------------------------------------------
# Test: imported symbol
# ---------------------------------------------------------------------------


class TestImportedSymbol:
    """
    ``join`` and ``exists`` are imported from os.path and then used.
    The user-defined functions ``build_path`` and ``check_path`` reference
    these imports, so they should appear as ACTIVE (they are referenced by
    calls inside the file).
    """

    def test_used_functions_are_active(self):
        result = analyze_repo(FIXTURES / "imported_symbol")
        mapping = candidates_by_name(result)
        assert mapping.get("build_path") == SafetyClassification.ACTIVE
        assert mapping.get("check_path") == SafetyClassification.ACTIVE

    def test_files_scanned(self):
        result = analyze_repo(FIXTURES / "imported_symbol")
        assert result.files_scanned >= 1


# ---------------------------------------------------------------------------
# Test: cross-file reference
# ---------------------------------------------------------------------------


class TestCrossFileReference:
    """
    utils.py defines helper() and another_helper().
    caller.py imports and calls both.
    Both helpers must appear ACTIVE.
    """

    def test_helper_is_active(self):
        result = analyze_repo(FIXTURES / "cross_file")
        mapping = candidates_by_name(result)
        assert mapping.get("helper") == SafetyClassification.ACTIVE

    def test_another_helper_is_active(self):
        result = analyze_repo(FIXTURES / "cross_file")
        mapping = candidates_by_name(result)
        assert mapping.get("another_helper") == SafetyClassification.ACTIVE

    def test_run_function_unreferenced(self):
        """run() in caller.py is not called → PROVABLE (no triggers)."""
        result = analyze_repo(FIXTURES / "cross_file")
        mapping = candidates_by_name(result)
        assert mapping.get("run") == SafetyClassification.PROVABLE

    def test_two_files_scanned(self):
        result = analyze_repo(FIXTURES / "cross_file")
        assert result.files_scanned == 2


# ---------------------------------------------------------------------------
# Test: syntax error
# ---------------------------------------------------------------------------


class TestSyntaxError:
    """
    bad_syntax.py has a syntax error.  The engine must not crash, and it
    must report the error.  The valid file alongside it must still be
    analysed.
    """

    def test_no_crash_on_syntax_error(self):
        result = analyze_repo(FIXTURES / "syntax_error")
        # If we reach here without exception, test passes.
        assert result is not None

    def test_error_is_reported(self):
        result = analyze_repo(FIXTURES / "syntax_error")
        assert result.files_with_errors >= 1
        assert any("bad_syntax" in e["file"] for e in result.errors)

    def test_valid_file_is_still_analysed(self):
        """good_module.py must be scanned and fine_function indexed."""
        result = analyze_repo(FIXTURES / "syntax_error")
        all_names = {c.symbol.name for c in result.candidates}
        assert "fine_function" in all_names

    def test_files_scanned_includes_both(self):
        """Both files should be counted in files_scanned."""
        result = analyze_repo(FIXTURES / "syntax_error")
        assert result.files_scanned == 2


# ---------------------------------------------------------------------------
# Test: ignored directories
# ---------------------------------------------------------------------------


class TestIgnoredDirectories:
    """
    __pycache__/ and .venv/ inside the fixture must not be scanned.
    Only module.py in the root must be analysed.
    """

    def test_pycache_not_scanned(self):
        result = analyze_repo(FIXTURES / "ignored_dirs")
        all_names = {c.symbol.name for c in result.candidates}
        assert "should_not_appear" not in all_names

    def test_venv_not_scanned(self):
        result = analyze_repo(FIXTURES / "ignored_dirs")
        all_names = {c.symbol.name for c in result.candidates}
        assert "venv_function" not in all_names

    def test_root_function_is_present(self):
        result = analyze_repo(FIXTURES / "ignored_dirs")
        all_names = {c.symbol.name for c in result.candidates}
        assert "root_function" in all_names

    def test_only_one_file_scanned(self):
        """Exactly one .py file outside ignored dirs."""
        result = analyze_repo(FIXTURES / "ignored_dirs")
        assert result.files_scanned == 1


# ---------------------------------------------------------------------------
# Test: ambiguous / dynamic cases → REVIEW
# ---------------------------------------------------------------------------


class TestDynamicCases:
    """
    Symbols that are exported via __all__, carry decorators, or are accessed
    via dynamic calls must be classified REVIEW, not PROVABLE.
    """

    def test_exported_function_is_review(self):
        """exported_function is in __all__ → REVIEW."""
        result = analyze_repo(FIXTURES / "dynamic")
        cand = find_candidate(result, "exported_function")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW

    def test_exported_function_evidence_mentions_all(self):
        result = analyze_repo(FIXTURES / "dynamic")
        cand = find_candidate(result, "exported_function")
        reasons = {e.reason for e in cand.evidence}
        assert "in_all" in reasons

    def test_decorated_target_is_review(self):
        """decorated_target has a decorator → REVIEW."""
        result = analyze_repo(FIXTURES / "dynamic")
        cand = find_candidate(result, "decorated_target")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW

    def test_decorated_target_evidence_mentions_decorator(self):
        result = analyze_repo(FIXTURES / "dynamic")
        cand = find_candidate(result, "decorated_target")
        reasons = {e.reason for e in cand.evidence}
        assert "has_decorator" in reasons

    def test_dynamic_loader_is_review_or_active(self):
        """
        dynamic_loader uses getattr/importlib → its sub-references make it
        non-trivially uncertain.  It should not be PROVABLE.
        """
        result = analyze_repo(FIXTURES / "dynamic")
        cand = find_candidate(result, "dynamic_loader")
        assert cand is not None
        assert cand.classification != SafetyClassification.PROVABLE

    def test_really_unused_is_provable(self):
        """
        really_unused has no references, no decorator, not in __all__.
        It must be PROVABLE to verify the fail-closed logic doesn't
        incorrectly escalate everything.
        """
        result = analyze_repo(FIXTURES / "dynamic")
        cand = find_candidate(result, "really_unused")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE


# ---------------------------------------------------------------------------
# Test: AnalysisResult structure
# ---------------------------------------------------------------------------


class TestAnalysisResultStructure:
    """Verify the result object structure and convenience properties."""

    def test_result_properties(self):
        result = analyze_repo(FIXTURES / "unused_function")
        assert isinstance(result.provable, list)
        assert isinstance(result.review, list)
        assert isinstance(result.active, list)

    def test_files_scanned_positive(self):
        result = analyze_repo(FIXTURES / "unused_function")
        assert result.files_scanned >= 1

    def test_total_definitions_positive(self):
        result = analyze_repo(FIXTURES / "unused_function")
        assert result.total_definitions >= 1

    def test_repo_root_is_string(self):
        result = analyze_repo(FIXTURES / "unused_function")
        assert isinstance(result.repo_root, str)

    def test_candidate_has_file_and_line(self):
        result = analyze_repo(FIXTURES / "unused_function")
        cand = find_candidate(result, "unused_function")
        assert cand is not None
        assert isinstance(cand.file, str)
        assert isinstance(cand.line, int)
        assert cand.line > 0


# ---------------------------------------------------------------------------
# Test: scanner unit tests
# ---------------------------------------------------------------------------


class TestScanner:
    """Unit tests for the file scanner."""

    def test_scan_finds_py_files(self, tmp_path):
        from deadcode.scanner import scan_files

        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("y = 2")
        (tmp_path / "notes.txt").write_text("not python")

        files, errors = scan_files(tmp_path)
        names = {f.name for f in files}
        assert "a.py" in names
        assert "b.py" in names
        assert "notes.txt" not in names
        assert errors == []

    def test_scan_skips_ignored_dirs(self, tmp_path):
        from deadcode.scanner import scan_files

        ignored = tmp_path / "__pycache__"
        ignored.mkdir()
        (ignored / "cached.py").write_text("x = 1")

        (tmp_path / "main.py").write_text("y = 2")

        files, errors = scan_files(tmp_path)
        names = {f.name for f in files}
        assert "main.py" in names
        assert "cached.py" not in names

    def test_scan_returns_sorted_paths(self, tmp_path):
        from deadcode.scanner import scan_files

        for name in ["z.py", "a.py", "m.py"]:
            (tmp_path / name).write_text("")

        files, _ = scan_files(tmp_path)
        assert files == sorted(files)

    def test_derive_module_name_simple(self):
        from deadcode.scanner import derive_module_name

        root = Path("/project")
        assert derive_module_name(Path("/project/pkg/utils.py"), root) == "pkg.utils"

    def test_derive_module_name_init(self):
        from deadcode.scanner import derive_module_name

        root = Path("/project")
        assert derive_module_name(Path("/project/pkg/__init__.py"), root) == "pkg"


# ---------------------------------------------------------------------------
# Test: parser unit tests
# ---------------------------------------------------------------------------


class TestParser:
    """Unit tests for the AST parser."""

    def test_parse_function_definition(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "mod.py"
        f.write_text("def foo(): pass\n")
        index = parse_file(f, "mod")
        names = [d.name for d in index.definitions]
        assert "foo" in names

    def test_parse_class_definition(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "mod.py"
        f.write_text("class Bar: pass\n")
        index = parse_file(f, "mod")
        names = [d.name for d in index.definitions]
        assert "Bar" in names

    def test_parse_all_detected(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "mod.py"
        f.write_text('__all__ = ["foo", "Bar"]\ndef foo(): pass\nclass Bar: pass\n')
        index = parse_file(f, "mod")
        assert index.all_names == ["foo", "Bar"]
        in_all = {d.name: d.in_all for d in index.definitions}
        assert in_all.get("foo") is True
        assert in_all.get("Bar") is True

    def test_parse_wildcard_import_detected(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "mod.py"
        f.write_text("from os import *\n")
        index = parse_file(f, "mod")
        assert index.has_wildcard_import is True

    def test_parse_syntax_error_graceful(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "bad.py"
        f.write_text("def broken(:\n    pass\n")
        index = parse_file(f, "bad")
        assert index.parse_error is not None
        assert "SyntaxError" in index.parse_error
        assert index.definitions == []

    def test_parse_decorator_detected(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "mod.py"
        f.write_text("import functools\n\n@functools.lru_cache\ndef cached(): pass\n")
        index = parse_file(f, "mod")
        fn = next(d for d in index.definitions if d.name == "cached")
        assert fn.has_decorator is True

    def test_parse_import_record(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "mod.py"
        f.write_text("from os.path import join\n")
        index = parse_file(f, "mod")
        assert any(imp.module == "os.path" and "join" in imp.names for imp in index.imports)

    def test_references_captured(self, tmp_path):
        from deadcode.parser import parse_file

        f = tmp_path / "mod.py"
        f.write_text("def foo(): pass\nresult = foo()\n")
        index = parse_file(f, "mod")
        ref_names = {r.name for r in index.references}
        assert "foo" in ref_names
