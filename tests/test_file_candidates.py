"""
tests/test_file_candidates.py – Comprehensive regression tests for Phase 2:
Python Unused File / Module Candidates.

Verifies:
1. Genuinely unused private/internal module -> PROVABLE
2. Production-imported module -> ACTIVE
3. Nested package imported module -> ACTIVE
4. Relative-imported module -> ACTIVE
5. Circular import -> ACTIVE (safely preserved)
6. Module imported only by another unused module -> importer PROVABLE, imported ACTIVE
7. __init__.py -> REVIEW (package_initialization)
8. __main__.py -> REVIEW (package_main_entrypoint)
9. if __name__ == "__main__": -> REVIEW (script_entrypoint)
10. Shebang script -> REVIEW (script_entrypoint)
11. conftest.py -> REVIEW (conftest_discovery)
12. test_foo.py -> REVIEW (test_infrastructure)
13. tests/helper.py -> REVIEW (test_infrastructure)
14. unittest TestCase module -> REVIEW (unittest_discovery)
15. pyproject console entrypoint -> REVIEW (config_entrypoint)
16. pyproject GUI entrypoint -> REVIEW (config_entrypoint)
17. pyproject plugin entrypoint -> REVIEW (config_entrypoint)
18. Syntax-error module -> REVIEW (file_parse_error)
19. Literal importlib target -> REVIEW (dynamic_import)
20. Unresolved dynamic import -> REVIEW (unresolved_dynamic_import)
21. Public library module without leading underscore -> REVIEW (public_api_module)
22. Private internal library module with leading underscore -> PROVABLE
23. Namespace package module (missing __init__.py) -> REVIEW (namespace_package)
24. Framework root script -> REVIEW (framework_convention)
25. Module imported only by tests -> REVIEW (only_test_references)
Plus focused parser tests for main block, shebang, and dynamic import extraction.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from deadcode import AnalysisResult, SafetyClassification, analyze_repo
from deadcode.models import SymbolKind
from deadcode.parser import parse_file


def find_module_candidate(result: AnalysisResult, name: str):
    """Return the first MODULE candidate whose bare or qualified name matches *name*, or None."""
    for c in result.candidates:
        if c.symbol.kind == SymbolKind.MODULE and (
            c.symbol.name == name or c.qualified_name == name
        ):
            return c
    return None


# ---------------------------------------------------------------------------
# Parser-level unit tests
# ---------------------------------------------------------------------------


class TestParserFileMetadata:
    """Unit tests for FileIndex metadata extracted during parsing."""

    def test_main_block_detected(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text('if __name__ == "__main__":\n    print("hello")\n')
        fi = parse_file(f, "script")
        assert fi.has_main_block is True

    def test_main_block_inverted_detected(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text('if "__main__" == __name__:\n    pass\n')
        fi = parse_file(f, "script")
        assert fi.has_main_block is True

    def test_arbitrary_compare_not_main_block(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text('if x == "__main__":\n    pass\n')
        fi = parse_file(f, "script")
        assert fi.has_main_block is False

    def test_shebang_detected(self, tmp_path):
        f = tmp_path / "tool.py"
        f.write_text('#!/usr/bin/env python3\nprint("hello")\n')
        fi = parse_file(f, "tool")
        assert fi.has_shebang is True

    def test_no_shebang(self, tmp_path):
        f = tmp_path / "tool.py"
        f.write_text('# just a comment\nprint("hello")\n')
        fi = parse_file(f, "tool")
        assert fi.has_shebang is False

    def test_literal_dynamic_import_target(self, tmp_path):
        f = tmp_path / "loader.py"
        f.write_text('import importlib\nimportlib.import_module("pkg.plugin")\n')
        fi = parse_file(f, "loader")
        assert "pkg.plugin" in fi.dynamic_import_targets
        assert fi.has_unresolved_dynamic_import is False

    def test_unresolved_dynamic_import_target(self, tmp_path):
        f = tmp_path / "loader.py"
        f.write_text('import importlib\nmod = "pkg.plugin"\nimportlib.import_module(mod)\n')
        fi = parse_file(f, "loader")
        assert fi.has_unresolved_dynamic_import is True


# ---------------------------------------------------------------------------
# 25 Scenarios for File Candidates
# ---------------------------------------------------------------------------


class TestFileCandidates:
    """Tests covering all 25 Phase 2 file candidate detection and classification scenarios."""

    # 1. Genuinely unused private/internal module -> PROVABLE
    def test_1_unused_private_module_is_provable(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "_legacy.py").write_text("def helper():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "_legacy")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE
        assert cand.ref_count == 0
        assert cand.test_ref_count == 0
        assert any(e.reason == "provable_unused" for e in cand.evidence)

    # 2. Production-imported module -> ACTIVE
    def test_2_production_imported_module_is_active(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "helper.py").write_text("def helper():\n    pass\n")
        (pkg / "app.py").write_text("import pkg.helper\ndef run():\n    pkg.helper.helper()\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "helper")
        assert cand is not None
        assert cand.classification == SafetyClassification.ACTIVE
        assert cand.ref_count > 0

    # 3. Nested package imported module -> ACTIVE
    def test_3_nested_package_imported_module_is_active(self, tmp_path):
        pkg = tmp_path / "pkg"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (sub / "__init__.py").write_text("")
        (sub / "nested.py").write_text("def do_work():\n    pass\n")
        (pkg / "app.py").write_text("from pkg.sub import nested\ndef run():\n    nested.do_work()\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "nested")
        assert cand is not None
        assert cand.classification == SafetyClassification.ACTIVE

    # 4. Relative-imported module -> ACTIVE
    def test_4_relative_imported_module_is_active(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "sibling.py").write_text("def sibling_fn():\n    pass\n")
        (pkg / "consumer.py").write_text("from . import sibling\ndef run():\n    sibling.sibling_fn()\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "sibling")
        assert cand is not None
        assert cand.classification == SafetyClassification.ACTIVE

    # 5. Circular import -> ACTIVE (safely preserved)
    def test_5_circular_imports_are_active(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("import pkg.b\ndef fn_a():\n    pass\n")
        (pkg / "b.py").write_text("import pkg.a\ndef fn_b():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand_a = find_module_candidate(result, "a")
        cand_b = find_module_candidate(result, "b")
        assert cand_a is not None and cand_a.classification == SafetyClassification.ACTIVE
        assert cand_b is not None and cand_b.classification == SafetyClassification.ACTIVE

    # 6. Module imported only by another unused module -> importer PROVABLE, imported ACTIVE
    def test_6_module_imported_only_by_unused_module(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "_target.py").write_text("def target():\n    pass\n")
        (pkg / "_unused_importer.py").write_text("import pkg._target\ndef run():\n    pkg._target.target()\n")

        result = analyze_repo(tmp_path)
        cand_importer = find_module_candidate(result, "_unused_importer")
        cand_target = find_module_candidate(result, "_target")
        assert cand_importer is not None
        assert cand_importer.classification == SafetyClassification.PROVABLE
        assert cand_target is not None
        assert cand_target.classification == SafetyClassification.ACTIVE

    # 7. __init__.py -> REVIEW (package_initialization)
    def test_7_init_py_is_review(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "__init__")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "package_initialization" in cand.uncertainty_reasons

    # 8. __main__.py -> REVIEW (package_main_entrypoint)
    def test_8_main_py_is_review(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text("print('running')\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "__main__")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "package_main_entrypoint" in cand.uncertainty_reasons

    # 9. if __name__ == "__main__": -> REVIEW (script_entrypoint)
    def test_9_main_block_is_review(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "_script.py").write_text('if __name__ == "__main__":\n    print("exec")\n')

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "_script")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "script_entrypoint" in cand.uncertainty_reasons

    # 10. Shebang script -> REVIEW (script_entrypoint)
    def test_10_shebang_script_is_review(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "_run.py").write_text('#!/usr/bin/env python3\nprint("exec")\n')

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "_run")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "script_entrypoint" in cand.uncertainty_reasons

    # 11. conftest.py -> REVIEW (conftest_discovery)
    def test_11_conftest_py_is_review(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "conftest.py").write_text("import pytest\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "conftest")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "conftest_discovery" in cand.uncertainty_reasons

    # 12. test_foo.py -> REVIEW (test_infrastructure)
    def test_12_test_file_is_review(self, tmp_path):
        (tmp_path / "test_foo.py").write_text("def test_one():\n    assert True\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "test_foo")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "test_infrastructure" in cand.uncertainty_reasons

    # 13. tests/helper.py -> REVIEW (test_infrastructure)
    def test_13_tests_helper_is_review(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "helper.py").write_text("def setup():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "helper")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "test_infrastructure" in cand.uncertainty_reasons

    # 14. unittest TestCase module -> REVIEW (unittest_discovery)
    def test_14_unittest_testcase_module_is_review(self, tmp_path):
        (tmp_path / "suite.py").write_text(textwrap.dedent("""\
            import unittest
            class MySuite(unittest.TestCase):
                def test_alpha(self):
                    pass
        """))

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "suite")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "unittest_discovery" in cand.uncertainty_reasons

    # 15. pyproject console entrypoint -> REVIEW (config_entrypoint)
    def test_15_pyproject_console_entrypoint_is_review(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "mytool"
            version = "0.1.0"
            [project.scripts]
            mytool = "pkg.cli:main"
        """))
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "cli.py").write_text("def main():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "cli")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "config_entrypoint" in cand.uncertainty_reasons

    # 16. pyproject GUI entrypoint -> REVIEW (config_entrypoint)
    def test_16_pyproject_gui_entrypoint_is_review(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "mygui"
            version = "0.1.0"
            [project.gui-scripts]
            mygui = "pkg.gui:main"
        """))
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "gui.py").write_text("def main():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "gui")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "config_entrypoint" in cand.uncertainty_reasons

    # 17. pyproject plugin entrypoint -> REVIEW (config_entrypoint)
    def test_17_pyproject_plugin_entrypoint_is_review(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "myplugin"
            version = "0.1.0"
            [project.entry-points."deadcode.plugins"]
            plug = "pkg.plugin:register"
        """))
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "plugin.py").write_text("def register():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "plugin")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "config_entrypoint" in cand.uncertainty_reasons

    # 18. Syntax-error module -> REVIEW (file_parse_error)
    def test_18_syntax_error_module_is_review(self, tmp_path):
        (tmp_path / "bad.py").write_text("def broken(:\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "bad")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "file_parse_error" in cand.uncertainty_reasons

    # 19. Literal importlib target -> REVIEW (dynamic_import)
    def test_19_literal_importlib_target_is_review(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "target.py").write_text("def run():\n    pass\n")
        (tmp_path / "loader.py").write_text('import importlib\nimportlib.import_module("pkg.target")\n')

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "target")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "dynamic_import" in cand.uncertainty_reasons

    # 20. Unresolved dynamic import -> REVIEW (unresolved_dynamic_import)
    def test_20_unresolved_dynamic_import_is_review(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "_target.py").write_text("def run():\n    pass\n")
        (tmp_path / "loader.py").write_text('import importlib\nx = "pkg._target"\nimportlib.import_module(x)\n')

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "_target")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "unresolved_dynamic_import" in cand.uncertainty_reasons

    # 21. Public library module without leading underscore -> REVIEW (public_api_module)
    def test_21_public_library_module_is_review(self, tmp_path):
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("")
        (src / "client.py").write_text("def api():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "client")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "public_api_module" in cand.uncertainty_reasons

    # 22. Private internal library module with leading underscore -> PROVABLE
    def test_22_private_internal_library_module_is_provable(self, tmp_path):
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("")
        (src / "_internal.py").write_text("def helper():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "_internal")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

    # 23. Namespace package module -> REVIEW (namespace_package)
    def test_23_namespace_package_module_is_review(self, tmp_path):
        ns = tmp_path / "nspkg"
        ns.mkdir()
        # Note: NO __init__.py in nspkg/
        (ns / "module.py").write_text("def fn():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "module")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "namespace_package" in cand.uncertainty_reasons

    # 24. Framework root script -> REVIEW (framework_convention)
    def test_24_framework_root_script_is_review(self, tmp_path):
        (tmp_path / "manage.py").write_text("def main():\n    pass\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "manage")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "framework_convention" in cand.uncertainty_reasons

    # 25. Module imported only by tests -> REVIEW (only_test_references)
    def test_25_module_imported_only_by_tests_is_review(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "_helper.py").write_text("def helper():\n    pass\n")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_something.py").write_text("import pkg._helper\ndef test_fn():\n    pkg._helper.helper()\n")

        result = analyze_repo(tmp_path)
        cand = find_module_candidate(result, "_helper")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW
        assert "only_test_references" in cand.uncertainty_reasons
