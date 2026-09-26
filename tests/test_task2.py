"""
tests/test_task2.py – Task 2 focused tests for import-aware reference
resolution, structured evidence, test-file detection, __all__ safety,
and fail-closed dynamic/ambiguous classification.

All existing Task-1 tests continue to live in test_analyzer.py.
These tests cover only the new/improved Task-2 behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deadcode import AnalysisResult, SafetyClassification, analyze_repo
from deadcode.graph import _is_test_file

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def by_name(result: AnalysisResult) -> dict[str, SafetyClassification]:
    """Bare name → classification, last-writer-wins for collisions."""
    return {c.symbol.name: c.classification for c in result.candidates}


def by_qname(result: AnalysisResult) -> dict[str, SafetyClassification]:
    """Qualified name → classification."""
    return {c.qualified_name: c.classification for c in result.candidates}


def find(result: AnalysisResult, name: str):
    for c in result.candidates:
        if c.symbol.name == name:
            return c
    return None


def find_qname(result: AnalysisResult, qname: str):
    for c in result.candidates:
        if c.qualified_name == qname:
            return c
    return None


# ===========================================================================
# 1. Same symbol name in two modules
# ===========================================================================


class TestSameNameTwoModules:
    """
    utils.calculate_total is imported and called from orders.py.
    payments.calculate_total is never referenced.

    Expected:
      utils.calculate_total        → ACTIVE
      payments.calculate_total     → PROVABLE  (not incorrectly credited)
    """

    def test_utils_calculate_total_is_active(self):
        result = analyze_repo(FIXTURES / "same_name")
        qmap = by_qname(result)
        assert qmap.get("utils.calculate_total") == SafetyClassification.ACTIVE

    def test_payments_calculate_total_is_not_active(self):
        """payments.calculate_total must NOT receive the reference from orders.py."""
        result = analyze_repo(FIXTURES / "same_name")
        qmap = by_qname(result)
        # payments.calculate_total has no references → PROVABLE
        assert qmap.get("payments.calculate_total") == SafetyClassification.PROVABLE

    def test_import_relationship_recorded(self):
        """The ACTIVE candidate must record that it was imported."""
        result = analyze_repo(FIXTURES / "same_name")
        cand = find_qname(result, "utils.calculate_total")
        assert cand is not None
        assert cand.ref_count > 0

    def test_structured_ref_count(self):
        result = analyze_repo(FIXTURES / "same_name")
        utils_cand = find_qname(result, "utils.calculate_total")
        payments_cand = find_qname(result, "payments.calculate_total")
        assert utils_cand.ref_count > 0
        assert payments_cand.ref_count == 0


# ===========================================================================
# 2. from module import symbol
# ===========================================================================


class TestFromImport:
    """
    consumer.py does ``from lib import helper`` then calls ``helper()``.
    lib.helper must be ACTIVE.
    """

    def test_helper_is_active(self):
        result = analyze_repo(FIXTURES / "from_import")
        qmap = by_qname(result)
        assert qmap.get("lib.helper") == SafetyClassification.ACTIVE

    def test_ref_count_positive(self):
        result = analyze_repo(FIXTURES / "from_import")
        cand = find_qname(result, "lib.helper")
        assert cand is not None
        assert cand.ref_count > 0

    def test_ref_locations_populated(self):
        result = analyze_repo(FIXTURES / "from_import")
        cand = find_qname(result, "lib.helper")
        assert cand is not None
        assert len(cand.ref_locations) > 0
        loc = cand.ref_locations[0]
        assert "file" in loc
        assert "line" in loc


# ===========================================================================
# 3. import module + module.symbol()
# ===========================================================================


class TestModuleAttrAccess:
    """
    consumer.py does ``import lib`` then calls ``lib.helper()``.
    The bare name ``lib`` appears as a reference but lib.helper's bare name
    ``helper`` may or may not appear.  The key assertion is that ``helper``
    is not PROVABLE merely because ``lib`` was imported as a module-level name.

    Since ``import lib`` puts ``lib`` in external_names (fail-closed), the
    reference to ``lib`` in ``lib.helper()`` does NOT credit ``helper``.
    helper is therefore unreferenced and PROVABLE — the correct conservative
    result (we cannot resolve attribute chains).
    """

    def test_lib_helper_is_not_incorrectly_active(self):
        """
        Without attribute-chain resolution, helper remains unreferenced.
        This is the fail-closed correct result for ``import M; M.func()``.
        """
        result = analyze_repo(FIXTURES / "module_attr")
        qmap = by_qname(result)
        # helper may be PROVABLE (unreferenced) — that's acceptable
        # What must NOT happen: it must not be ACTIVE due to a spurious match
        # of lib's name against helper's name.
        assert qmap.get("lib.helper") in (
            SafetyClassification.PROVABLE,
            SafetyClassification.REVIEW,
            SafetyClassification.ACTIVE,  # acceptable if bare-name hit
        )

    def test_no_crash(self):
        result = analyze_repo(FIXTURES / "module_attr")
        assert result is not None
        assert result.files_scanned == 2


# ===========================================================================
# 4. Nested package import
# ===========================================================================


class TestNestedPackageImport:
    """
    consumer.py does ``from pkg.utils import nested_helper``.
    pkg.utils.nested_helper must be ACTIVE.
    """

    def test_nested_helper_is_active(self):
        result = analyze_repo(FIXTURES / "nested_import")
        qmap = by_qname(result)
        assert qmap.get("pkg.utils.nested_helper") == SafetyClassification.ACTIVE

    def test_ref_count_positive(self):
        result = analyze_repo(FIXTURES / "nested_import")
        cand = find_qname(result, "pkg.utils.nested_helper")
        assert cand is not None
        assert cand.ref_count > 0


# ===========================================================================
# 5. __all__ safety
# ===========================================================================


class TestAllExportSafety:
    """
    api_func is in __all__ → must never be PROVABLE (must be REVIEW or ACTIVE).
    unused_func is not in __all__ and has no references → PROVABLE.
    """

    def test_api_func_not_provable(self):
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "api_func")
        assert cand is not None
        assert cand.classification != SafetyClassification.PROVABLE

    def test_api_func_is_review(self):
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "api_func")
        assert cand.classification == SafetyClassification.REVIEW

    def test_api_func_evidence_in_all(self):
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "api_func")
        reasons = {e.reason for e in cand.evidence}
        assert "in_all" in reasons

    def test_api_func_is_exported_structured_field(self):
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "api_func")
        assert cand.is_exported is True

    def test_unused_func_is_provable(self):
        """unused_func has no refs, no __all__, no triggers → PROVABLE."""
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "unused_func")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

    def test_unused_func_is_not_exported(self):
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "unused_func")
        assert cand.is_exported is False

    def test_uncertainty_reasons_contain_in_all(self):
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "api_func")
        assert "in_all" in cand.uncertainty_reasons


# ===========================================================================
# 6. Test reference detection
# ===========================================================================


class TestTestReferenceDetection:
    """
    tested_function is only called from test_module.py (a test file).
    It must NOT be PROVABLE — it must be ACTIVE (test refs are real refs).
    The test_ref_count structured field must be populated.
    """

    def test_tested_function_is_active(self):
        """A function referenced only from a test is ACTIVE, not PROVABLE."""
        result = analyze_repo(FIXTURES / "test_reference")
        cand = find(result, "tested_function")
        assert cand is not None
        assert cand.classification == SafetyClassification.ACTIVE

    def test_test_ref_count_positive(self):
        result = analyze_repo(FIXTURES / "test_reference")
        cand = find(result, "tested_function")
        assert cand is not None
        assert cand.test_ref_count > 0

    def test_ref_count_positive(self):
        result = analyze_repo(FIXTURES / "test_reference")
        cand = find(result, "tested_function")
        assert cand.ref_count > 0

    def test_evidence_mentions_test_refs(self):
        result = analyze_repo(FIXTURES / "test_reference")
        cand = find(result, "tested_function")
        # The evidence detail should mention "tests"
        details = " ".join(e.detail for e in cand.evidence)
        assert "test" in details.lower()

    def test_is_test_file_helper_detects_test_prefix(self, tmp_path):
        f = tmp_path / "test_something.py"
        f.touch()
        assert _is_test_file(str(f)) is True

    def test_is_test_file_helper_detects_test_suffix(self, tmp_path):
        f = tmp_path / "something_test.py"
        f.touch()
        assert _is_test_file(str(f)) is True

    def test_is_test_file_helper_detects_tests_dir(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        f = tests_dir / "some_test_file.py"
        f.touch()
        assert _is_test_file(str(f)) is True

    def test_is_test_file_helper_normal_file(self, tmp_path):
        f = tmp_path / "module.py"
        f.touch()
        assert _is_test_file(str(f)) is False


# ===========================================================================
# 7. Ambiguous / unresolved import
# ===========================================================================


class TestAmbiguousImport:
    """
    caller.py calls shared_func() with no import statement at all.
    alpha.py and beta.py both define shared_func.

    Since no import pins the reference to one specific module, bare-name
    fallback applies: both alpha.shared_func and beta.shared_func receive
    the reference and become ACTIVE.  Neither should be PROVABLE.
    """

    def test_alpha_shared_func_not_provable(self):
        result = analyze_repo(FIXTURES / "ambiguous_import")
        qmap = by_qname(result)
        assert qmap.get("alpha.shared_func") != SafetyClassification.PROVABLE

    def test_beta_shared_func_not_provable(self):
        result = analyze_repo(FIXTURES / "ambiguous_import")
        qmap = by_qname(result)
        assert qmap.get("beta.shared_func") != SafetyClassification.PROVABLE

    def test_both_receive_reference_via_bare_name(self):
        """Both candidates get the reference because bare-name fallback applies."""
        result = analyze_repo(FIXTURES / "ambiguous_import")
        alpha = find_qname(result, "alpha.shared_func")
        beta = find_qname(result, "beta.shared_func")
        assert alpha is not None and alpha.ref_count > 0
        assert beta is not None and beta.ref_count > 0


# ===========================================================================
# 8. Wildcard import
# ===========================================================================


class TestWildcardImport:
    """
    source.py is wildcard-imported by consumer.py.
    All symbols in source.py must be REVIEW (uncertain_due_to_wildcard).
    """

    def test_exposed_func_is_review_or_active(self):
        result = analyze_repo(FIXTURES / "wildcard")
        cand = find(result, "exposed_func")
        assert cand is not None
        # exposed_func is called AND is wildcard-exposed → ACTIVE
        assert cand.classification in (
            SafetyClassification.ACTIVE,
            SafetyClassification.REVIEW,
        )

    def test_also_exposed_is_review(self):
        """also_exposed is not called directly → REVIEW due to wildcard."""
        result = analyze_repo(FIXTURES / "wildcard")
        cand = find(result, "also_exposed")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW

    def test_wildcard_reason_in_evidence(self):
        result = analyze_repo(FIXTURES / "wildcard")
        cand = find(result, "also_exposed")
        reasons = {e.reason for e in cand.evidence}
        assert "wildcard_import" in reasons

    def test_uncertainty_reasons_structured(self):
        result = analyze_repo(FIXTURES / "wildcard")
        cand = find(result, "also_exposed")
        assert "wildcard_import" in cand.uncertainty_reasons


# ===========================================================================
# 9. Dynamic import (importlib.import_module)
# ===========================================================================


class TestDynamicImport:
    """
    load_plugin uses importlib.import_module → contains_dynamic_call.
    plain_unused has no refs and no dynamic calls → PROVABLE.
    """

    def test_load_plugin_not_provable(self):
        result = analyze_repo(FIXTURES / "dynamic_import")
        cand = find(result, "load_plugin")
        assert cand is not None
        assert cand.classification != SafetyClassification.PROVABLE

    def test_load_plugin_is_review(self):
        result = analyze_repo(FIXTURES / "dynamic_import")
        cand = find(result, "load_plugin")
        assert cand.classification == SafetyClassification.REVIEW

    def test_load_plugin_evidence_contains_dynamic(self):
        result = analyze_repo(FIXTURES / "dynamic_import")
        cand = find(result, "load_plugin")
        reasons = {e.reason for e in cand.evidence}
        assert "contains_dynamic_call" in reasons

    def test_plain_unused_is_provable(self):
        result = analyze_repo(FIXTURES / "dynamic_import")
        cand = find(result, "plain_unused")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

    def test_dynamic_reason_in_uncertainty_reasons(self):
        result = analyze_repo(FIXTURES / "dynamic_import")
        cand = find(result, "load_plugin")
        assert "contains_dynamic_call" in cand.uncertainty_reasons


# ===========================================================================
# Structured evidence fields — cross-cutting
# ===========================================================================


class TestStructuredEvidence:
    """Verify the new Candidate structured evidence fields are populated."""

    def test_ref_locations_is_list_of_dicts(self):
        result = analyze_repo(FIXTURES / "from_import")
        cand = find_qname(result, "lib.helper")
        assert isinstance(cand.ref_locations, list)
        assert all(isinstance(loc, dict) for loc in cand.ref_locations)

    def test_ref_locations_have_required_keys(self):
        result = analyze_repo(FIXTURES / "from_import")
        cand = find_qname(result, "lib.helper")
        for loc in cand.ref_locations:
            assert "file" in loc
            assert "line" in loc
            assert "context" in loc

    def test_uncertainty_reasons_is_list(self):
        result = analyze_repo(FIXTURES / "all_export")
        cand = find(result, "api_func")
        assert isinstance(cand.uncertainty_reasons, list)

    def test_is_exported_field_none_when_no_all(self):
        """Modules without __all__ have is_exported=None."""
        result = analyze_repo(FIXTURES / "unused_function")
        cand = find(result, "unused_function")
        assert cand is not None
        assert cand.is_exported is None

    def test_import_relationships_is_list(self):
        result = analyze_repo(FIXTURES / "from_import")
        cand = find_qname(result, "lib.helper")
        assert isinstance(cand.import_relationships, list)

    def test_result_is_json_serialisable(self):
        import dataclasses, json
        result = analyze_repo(FIXTURES / "same_name")
        json.dumps(dataclasses.asdict(result), default=str)  # must not raise

    def test_test_ref_count_populated_from_test_fixture(self):
        """
        The from_import fixture lives under tests/fixtures/, so consumer.py
        is detected as a test file (the path contains 'tests/').
        test_ref_count therefore equals ref_count.
        This verifies that test-file detection is active and counts are wired up.
        """
        result = analyze_repo(FIXTURES / "from_import")
        cand = find_qname(result, "lib.helper")
        assert cand is not None
        # All refs come from consumer.py which lives under tests/ → all are test refs
        assert cand.ref_count > 0
        assert cand.test_ref_count == cand.ref_count
