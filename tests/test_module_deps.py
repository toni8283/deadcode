"""
tests/test_module_deps.py – Tests for Phase 1: Module-level dependency resolution.

Verifies:
1. ast.ImportFrom level preserved in ImportRecord (0 for absolute, 1 for '.', 2 for '..').
2. Module dependencies for 'import M', 'import M.sub', and 'import M.sub as alias'.
3. Submodule resolution for 'from pkg import submodule'.
4. Symbol resolution for 'from pkg import func'.
5. Relative import resolution for 'from .sub import ...' and 'from . import ...'.
6. Parent-relative import resolution for 'from ..other import ...'.
7. Circular module dependencies handled cleanly without recursion.
8. Self-dependencies rejected (never recorded).
9. External imports (os, sys, etc.) do not produce local module dependency edges.
10. Fail-closed behavior for relative imports beyond the top-level package.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from deadcode.graph import _resolve_relative_module, build_graph
from deadcode.indexer import build_index
from deadcode.parser import parse_file


class TestImportRecordLevel:
    """Verifies that ImportRecord accurately stores the relative import level."""

    def test_import_levels(self, tmp_path):
        src = tmp_path / "sample.py"
        src.write_text(
            textwrap.dedent("""\
            import os
            from os import path
            from . import sibling
            from .sub import helper
            from ..other import something
            from ...deep import value
            """)
        )
        fi = parse_file(src, "sample")
        imports_by_stmt = {
            (imp.module, tuple(imp.names)): imp.level for imp in fi.imports
        }

        # import os -> level 0
        assert imports_by_stmt[("os", ())] == 0
        # from os import path -> level 0
        assert imports_by_stmt[("os", ("path",))] == 0
        # from . import sibling -> level 1
        assert imports_by_stmt[("", ("sibling",))] == 1
        # from .sub import helper -> level 1
        assert imports_by_stmt[("sub", ("helper",))] == 1
        # from ..other import something -> level 2
        assert imports_by_stmt[("other", ("something",))] == 2
        # from ...deep import value -> level 3
        assert imports_by_stmt[("deep", ("value",))] == 3


class TestRelativeModuleResolution:
    """Unit tests for _resolve_relative_module helper."""

    def test_non_init_module_relative_import(self, tmp_path):
        p = tmp_path / "pkg" / "foo.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        fi = parse_file(p, "pkg.foo")

        # level 1: current package (pkg)
        assert _resolve_relative_module("bar", 1, fi) == "pkg.bar"
        assert _resolve_relative_module("", 1, fi) == "pkg"

        # level 2: ascends beyond pkg (pkg has 1 part) -> None
        assert _resolve_relative_module("bar", 2, fi) is None

    def test_init_module_relative_import(self, tmp_path):
        p = tmp_path / "pkg" / "__init__.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        fi = parse_file(p, "pkg")

        # level 1: current package (pkg)
        assert _resolve_relative_module("bar", 1, fi) == "pkg.bar"
        assert _resolve_relative_module("", 1, fi) == "pkg"

        # level 2: ascends beyond pkg -> None
        assert _resolve_relative_module("bar", 2, fi) is None

    def test_nested_module_relative_import(self, tmp_path):
        p = tmp_path / "pkg" / "sub" / "foo.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        fi = parse_file(p, "pkg.sub.foo")

        # level 1: pkg.sub
        assert _resolve_relative_module("bar", 1, fi) == "pkg.sub.bar"
        # level 2: pkg
        assert _resolve_relative_module("other", 2, fi) == "pkg.other"
        assert _resolve_relative_module("", 2, fi) == "pkg"
        # level 3: beyond pkg -> None
        assert _resolve_relative_module("other", 3, fi) is None

    def test_top_level_module_relative_import_fails(self, tmp_path):
        p = tmp_path / "main.py"
        p.touch()
        fi = parse_file(p, "main")

        assert _resolve_relative_module("bar", 1, fi) is None


class TestModuleLevelDependencies:
    """Verifies that ReferenceGraph accurately tracks module dependencies."""

    def test_absolute_and_dotted_imports(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "sub.py").write_text("def sub_fn(): pass\n")
        (tmp_path / "main.py").write_text(
            textwrap.dedent("""\
            import pkg
            import pkg.sub
            import pkg.sub as aliased_sub
            import os  # external
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        deps = graph.get_module_dependencies("main")
        assert "pkg" in deps
        assert "pkg.sub" in deps
        assert "os" not in deps  # External module, no local edge

        incoming_pkg = graph.get_incoming_module_dependencies("pkg")
        assert "main" in incoming_pkg

        incoming_sub = graph.get_incoming_module_dependencies("pkg.sub")
        assert "main" in incoming_sub

    def test_from_submodule_import(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "worker.py").write_text("def do_work(): pass\n")
        (tmp_path / "consumer.py").write_text(
            textwrap.dedent("""\
            from pkg import worker

            worker.do_work()
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        deps = graph.get_module_dependencies("consumer")
        assert "pkg.worker" in deps

        incoming = graph.get_incoming_module_dependencies("pkg.worker")
        assert "consumer" in incoming

    def test_from_symbol_import(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def helper(): pass\n")
        (tmp_path / "consumer.py").write_text(
            textwrap.dedent("""\
            from pkg import helper

            helper()
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        deps = graph.get_module_dependencies("consumer")
        assert "pkg" in deps
        assert "consumer" in graph.get_incoming_module_dependencies("pkg")

    def test_relative_import_same_package(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "helper.py").write_text("def util(): pass\n")
        (pkg / "worker.py").write_text(
            textwrap.dedent("""\
            from . import helper
            from .helper import util

            util()
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        deps = graph.get_module_dependencies("pkg.worker")
        assert "pkg.helper" in deps
        assert "pkg.worker" in graph.get_incoming_module_dependencies("pkg.helper")

    def test_parent_relative_import(self, tmp_path):
        pkg = tmp_path / "pkg"
        nested = pkg / "nested"
        nested.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "common.py").write_text("def common_fn(): pass\n")
        (nested / "__init__.py").write_text("")
        (nested / "child.py").write_text(
            textwrap.dedent("""\
            from ..common import common_fn
            from .. import common

            common_fn()
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        deps = graph.get_module_dependencies("pkg.nested.child")
        assert "pkg.common" in deps
        assert "pkg.nested.child" in graph.get_incoming_module_dependencies("pkg.common")

    def test_relative_import_to_init(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def init_helper(): pass\n")
        (pkg / "client.py").write_text(
            textwrap.dedent("""\
            from . import init_helper

            init_helper()
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        deps = graph.get_module_dependencies("pkg.client")
        assert "pkg" in deps
        assert "pkg.client" in graph.get_incoming_module_dependencies("pkg")

    def test_circular_imports_handled_safely(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "mod_a.py").write_text(
            textwrap.dedent("""\
            from . import mod_b
            def a_fn(): pass
            """)
        )
        (pkg / "mod_b.py").write_text(
            textwrap.dedent("""\
            from . import mod_a
            def b_fn(): pass
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        assert graph.get_module_dependencies("pkg.mod_a") == {"pkg.mod_b"}
        assert graph.get_module_dependencies("pkg.mod_b") == {"pkg.mod_a"}
        assert graph.get_incoming_module_dependencies("pkg.mod_a") == {"pkg.mod_b"}
        assert graph.get_incoming_module_dependencies("pkg.mod_b") == {"pkg.mod_a"}

    def test_self_dependency_ignored(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "myself.py").write_text(
            textwrap.dedent("""\
            import pkg.myself
            from . import myself
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        assert "pkg.myself" not in graph.get_module_dependencies("pkg.myself")
        assert "pkg.myself" not in graph.get_incoming_module_dependencies("pkg.myself")

    def test_wildcard_import_dependency(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "constants.py").write_text("X = 1\nY = 2\n")
        (tmp_path / "user.py").write_text(
            textwrap.dedent("""\
            from pkg.constants import *
            """)
        )

        index, _ = build_index(tmp_path)
        graph = build_graph(index)

        assert "pkg.constants" in graph.get_module_dependencies("user")
        assert "user" in graph.get_incoming_module_dependencies("pkg.constants")
