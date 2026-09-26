"""
tests/test_safety_hardening.py - Regression tests for framework-aware safety hardening.

Verifies:
1. pytest test functions, classes, and methods -> REVIEW (test_discovery)
2. test_* functions in non-test files remain PROVABLE
3. conftest.py symbols -> REVIEW (conftest_discovery)
4. ast.NodeVisitor and ast.NodeTransformer visit_* methods -> REVIEW (visitor_dispatch)
5. Ordinary unused visit_* methods in unrelated classes/modules remain PROVABLE
6. unittest.TestCase test_* and lifecycle methods -> REVIEW (unittest_discovery)
7. unittest.TestCase subclass itself -> REVIEW (unittest_discovery)
8. pyproject.toml entrypoints -> REVIEW (config_entrypoint)
9. Genuinely unused ordinary production functions remain PROVABLE
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from deadcode import AnalysisResult, SafetyClassification, analyze_repo


def find_candidate(result: AnalysisResult, name: str):
    """Return the first candidate whose bare or qualified name matches *name*, or None."""
    for c in result.candidates:
        if c.symbol.name == name or c.qualified_name == name:
            return c
    return None


class TestPytestDiscoverySafety:
    """pytest conventions (test_* functions/methods, Test* classes) in test files."""

    def test_pytest_function_in_test_file(self, tmp_path):
        test_file = tmp_path / "test_example.py"
        test_file.write_text("def test_something():\n    assert True\n")
        result = analyze_repo(tmp_path)
        cand = find_candidate(result, "test_something")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "test_discovery" in cand.uncertainty_reasons

    def test_pytest_class_in_test_file(self, tmp_path):
        test_file = tmp_path / "test_example.py"
        test_file.write_text("class TestWorkflow:\n    pass\n")
        result = analyze_repo(tmp_path)
        cand = find_candidate(result, "TestWorkflow")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "test_discovery" in cand.uncertainty_reasons

    def test_pytest_method_in_test_class(self, tmp_path):
        test_file = tmp_path / "test_example.py"
        test_file.write_text(textwrap.dedent("""\
            class TestWorkflow:
                def test_step_one(self):
                    pass
        """))
        result = analyze_repo(tmp_path)
        class_cand = find_candidate(result, "TestWorkflow")
        method_cand = find_candidate(result, "test_step_one")
        assert class_cand is not None
        assert class_cand.classification == SafetyClassification.REVIEW
        assert "test_discovery" in class_cand.uncertainty_reasons
        assert method_cand is not None
        assert method_cand.classification == SafetyClassification.REVIEW
        assert "test_discovery" in method_cand.uncertainty_reasons

    def test_test_prefix_in_non_test_file_remains_provable(self, tmp_path):
        prod_file = tmp_path / "network_utils.py"
        prod_file.write_text("def test_connection():\n    return True\n")
        result = analyze_repo(tmp_path)
        cand = find_candidate(result, "test_connection")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE
        assert "test_discovery" not in cand.uncertainty_reasons


class TestConftestDiscoverySafety:
    """Symbols in conftest.py must be classified as REVIEW."""

    def test_conftest_symbol(self, tmp_path):
        conftest = tmp_path / "conftest.py"
        conftest.write_text(textwrap.dedent("""\
            def global_fixture():
                return 42

            GLOBAL_CONFIG = {"debug": True}
        """))
        result = analyze_repo(tmp_path)
        fn_cand = find_candidate(result, "global_fixture")
        var_cand = find_candidate(result, "GLOBAL_CONFIG")
        assert fn_cand is not None
        assert fn_cand.classification == SafetyClassification.REVIEW
        assert "conftest_discovery" in fn_cand.uncertainty_reasons
        assert var_cand is not None
        assert var_cand.classification == SafetyClassification.REVIEW
        assert "conftest_discovery" in var_cand.uncertainty_reasons


class TestAstVisitorDispatchSafety:
    """visit_* methods on NodeVisitor and NodeTransformer subclasses."""

    def test_ast_node_visitor_dispatch(self, tmp_path):
        visitor_file = tmp_path / "visitor.py"
        visitor_file.write_text(textwrap.dedent("""\
            import ast

            class CustomVisitor(ast.NodeVisitor):
                def visit_Name(self, node):
                    pass

                def visit_Call(self, node):
                    pass
        """))
        result = analyze_repo(tmp_path)
        for method_name in ("visit_Name", "visit_Call"):
            cand = find_candidate(result, method_name)
            assert cand is not None
            assert cand.classification == SafetyClassification.REVIEW
            assert "visitor_dispatch" in cand.uncertainty_reasons

    def test_ast_node_transformer_dispatch(self, tmp_path):
        transformer_file = tmp_path / "transformer.py"
        transformer_file.write_text(textwrap.dedent("""\
            from ast import NodeTransformer

            class RewriteTransformer(NodeTransformer):
                def visit_Constant(self, node):
                    return node
        """))
        result = analyze_repo(tmp_path)
        cand = find_candidate(result, "visit_Constant")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "visitor_dispatch" in cand.uncertainty_reasons

    def test_ordinary_unused_visit_method_remains_provable(self, tmp_path):
        service_file = tmp_path / "service.py"
        service_file.write_text(textwrap.dedent("""\
            class GuestBook:
                def visit_profile(self):
                    pass

            def visit_homepage():
                pass
        """))
        result = analyze_repo(tmp_path)
        method_cand = find_candidate(result, "visit_profile")
        fn_cand = find_candidate(result, "visit_homepage")
        assert method_cand is not None
        assert method_cand.classification == SafetyClassification.PROVABLE
        assert "visitor_dispatch" not in method_cand.uncertainty_reasons
        assert fn_cand is not None
        assert fn_cand.classification == SafetyClassification.PROVABLE
        assert "visitor_dispatch" not in fn_cand.uncertainty_reasons


class TestUnittestDiscoverySafety:
    """unittest.TestCase subclasses, test_* methods, and lifecycle methods."""

    def test_unittest_testcase_test_method(self, tmp_path):
        test_file = tmp_path / "test_suite.py"
        test_file.write_text(textwrap.dedent("""\
            import unittest

            class MathSuite(unittest.TestCase):
                def test_addition(self):
                    self.assertEqual(1 + 1, 2)
        """))
        result = analyze_repo(tmp_path)
        cand = find_candidate(result, "test_addition")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "unittest_discovery" in cand.uncertainty_reasons

    def test_unittest_lifecycle_methods(self, tmp_path):
        test_file = tmp_path / "test_lifecycle.py"
        test_file.write_text(textwrap.dedent("""\
            from unittest import TestCase

            class DatabaseSuite(TestCase):
                def setUp(self):
                    pass

                def tearDown(self):
                    pass

                def setUpClass(cls):
                    pass

                def tearDownClass(cls):
                    pass
        """))
        result = analyze_repo(tmp_path)
        for lifecycle_name in ("setUp", "tearDown", "setUpClass", "tearDownClass"):
            cand = find_candidate(result, lifecycle_name)
            assert cand is not None
            assert cand.classification == SafetyClassification.REVIEW
            assert "unittest_discovery" in cand.uncertainty_reasons

    def test_unittest_testcase_class_itself(self, tmp_path):
        test_file = tmp_path / "test_base.py"
        test_file.write_text(textwrap.dedent("""\
            import unittest

            class BaseIntegrationTest(unittest.TestCase):
                pass
        """))
        result = analyze_repo(tmp_path)
        cand = find_candidate(result, "BaseIntegrationTest")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "unittest_discovery" in cand.uncertainty_reasons


class TestConfigEntrypointSafety:
    """pyproject.toml entrypoints should be classified as REVIEW."""

    def test_config_entrypoint(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "demo"
            version = "0.1.0"

            [project.scripts]
            demo-cli = "demo.cli:main"
        """))
        pkg_dir = tmp_path / "demo"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "cli.py").write_text("def main():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_candidate(result, "demo.cli.main")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "config_entrypoint" in cand.uncertainty_reasons


class TestPositiveControl:
    """Ordinary unreferenced production functions must remain PROVABLE."""

    def test_genuinely_unused_ordinary_production_function(self, tmp_path):
        app_file = tmp_path / "app.py"
        app_file.write_text(textwrap.dedent("""\
            def genuinely_unused_helper():
                return 42

            def active_helper():
                return 1

            result = active_helper()
        """))
        result = analyze_repo(tmp_path)
        unused_cand = find_candidate(result, "genuinely_unused_helper")
        active_cand = find_candidate(result, "active_helper")
        assert unused_cand is not None
        assert unused_cand.classification == SafetyClassification.PROVABLE
        assert active_cand is not None
        assert active_cand.classification == SafetyClassification.ACTIVE
