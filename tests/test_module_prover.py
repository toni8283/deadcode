"""
tests/test_module_prover.py - Verification and removal tests for SymbolKind.MODULE candidates.

Covers:
1. ACTIVE module -> BLOCKED
2. REVIEW module -> BLOCKED
3. Genuinely unused private module -> PROVEN
4. Working tree unchanged after prove
5. Deletion that breaks pytest -> FAILED
6. Deletion that breaks mypy -> FAILED
7. remove_candidate() unlinks module and returns line count
8. Non-Python removal rejected
9. Outside-worktree removal rejected
10. Apply proven module deletes exact file and formats output correctly
11. Apply unproven candidate -> BLOCKED
12. Stale / mismatched proof -> BLOCKED and file unchanged
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deadcode import analyze_repo
from deadcode.cli import app
from deadcode.models import (
    Candidate,
    Location,
    SafetyClassification,
    SymbolDef,
    SymbolKind,
)
from deadcode.proof_models import ProofResult, ProofStatus, VerificationStatus
from deadcode.prover import ProofOptions, prove_candidate
from deadcode.remover import RemovalError, remove_candidate
from deadcode.state import get_proof, record_proof

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git not found on PATH - proof engine tests require git",
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> None:
    _git(["git", "init", "-b", "main"], path)
    _git(["git", "config", "user.email", "test@deadcode.test"], path)
    _git(["git", "config", "user.name", "DeadCode Test"], path)


def _commit(path: Path, message: str = "init") -> None:
    _git(["git", "add", "."], path)
    _git(["git", "commit", "-m", message, "--allow-empty"], path)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _find_module_candidate(repo: Path, qualified_name: str) -> Candidate | None:
    result = analyze_repo(repo)
    for c in result.candidates:
        if c.symbol.kind == SymbolKind.MODULE and c.symbol.qualified_name == qualified_name:
            return c
    return None


# ===========================================================================
# Remover Unit Tests for SymbolKind.MODULE
# ===========================================================================


class TestRemoverModuleUnit:
    def test_remove_candidate_unlinks_module_and_returns_line_count(self, tmp_path):
        f = tmp_path / "pkg" / "_orphan.py"
        _write(
            f,
            """\
            # 1
            # 2
            def orphan():
                return 42
            """,
        )
        cand = Candidate(
            symbol=SymbolDef(
                name="_orphan",
                qualified_name="pkg._orphan",
                kind=SymbolKind.MODULE,
                location=Location(file=str(f), line=1),
            ),
            classification=SafetyClassification.PROVABLE,
        )

        lines_removed = remove_candidate(cand, tmp_path)
        assert lines_removed == 4
        assert not f.exists()

    def test_non_python_removal_rejected(self, tmp_path):
        f = tmp_path / "data.txt"
        _write(f, "some text\n")
        cand = Candidate(
            symbol=SymbolDef(
                name="data",
                qualified_name="data",
                kind=SymbolKind.MODULE,
                location=Location(file=str(f), line=1),
            ),
            classification=SafetyClassification.PROVABLE,
        )

        with pytest.raises(RemovalError, match="Refusing to remove non-Python file"):
            remove_candidate(cand, tmp_path)
        assert f.exists()

    def test_outside_worktree_removal_rejected(self, tmp_path):
        outside_file = tmp_path / "outside.py"
        _write(outside_file, "print('outside')\n")

        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        symlink_file = worktree_dir / "link.py"
        try:
            symlink_file.symlink_to(outside_file)
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        cand = Candidate(
            symbol=SymbolDef(
                name="link",
                qualified_name="link",
                kind=SymbolKind.MODULE,
                location=Location(file=str(symlink_file), line=1),
            ),
            classification=SafetyClassification.PROVABLE,
        )

        with pytest.raises(RemovalError, match="outside worktree root"):
            remove_candidate(cand, worktree_dir)
        assert outside_file.exists()

    def test_missing_module_file_rejected(self, tmp_path):
        f = tmp_path / "nonexistent.py"
        cand = Candidate(
            symbol=SymbolDef(
                name="nonexistent",
                qualified_name="nonexistent",
                kind=SymbolKind.MODULE,
                location=Location(file=str(f), line=1),
            ),
            classification=SafetyClassification.PROVABLE,
        )

        with pytest.raises(RemovalError, match="does not exist"):
            remove_candidate(cand, tmp_path)


# ===========================================================================
# Prover Tests for SymbolKind.MODULE
# ===========================================================================


class TestModuleProverWorkflow:
    def test_active_module_blocked_from_proof(self, tmp_path):
        _init_repo(tmp_path)
        _write(
            tmp_path / "pkg" / "__init__.py",
            ""
        )
        _write(
            tmp_path / "pkg" / "main.py",
            """\
            import pkg._worker
            pkg._worker.work()
            """,
        )
        _write(
            tmp_path / "pkg" / "_worker.py",
            """\
            def work():
                pass
            """,
        )
        _commit(tmp_path)

        cand = _find_module_candidate(tmp_path, "pkg._worker")
        assert cand is not None
        assert cand.classification == SafetyClassification.ACTIVE

        result = prove_candidate(tmp_path, cand)
        assert result.status == ProofStatus.BLOCKED
        assert "PROVABLE" in result.failure_reason

    def test_review_module_blocked_from_proof(self, tmp_path):
        _init_repo(tmp_path)
        _write(
            tmp_path / "pkg" / "__init__.py",
            ""
        )
        _write(
            tmp_path / "pkg" / "_helper.py",
            """\
            def help_me():
                return 1
            """,
        )
        _write(
            tmp_path / "tests" / "test_pkg.py",
            """\
            from pkg import _helper
            def test_help():
                assert _helper.help_me() == 1
            """,
        )
        _commit(tmp_path)

        cand = _find_module_candidate(tmp_path, "pkg._helper")
        assert cand is not None
        assert cand.classification == SafetyClassification.REVIEW

        result = prove_candidate(tmp_path, cand)
        assert result.status == ProofStatus.BLOCKED
        assert "PROVABLE" in result.failure_reason

    def test_genuinely_unused_private_module_proven(self, tmp_path):
        _init_repo(tmp_path)
        _write(
            tmp_path / "pkg" / "__init__.py",
            ""
        )
        _write(
            tmp_path / "pkg" / "app.py",
            """\
            def run():
                return 42
            """,
        )
        _write(
            tmp_path / "pkg" / "_unused.py",
            """\
            # Obsolete internal helper
            def dead():
                return "goodbye"
            """,
        )
        _commit(tmp_path)

        cand = _find_module_candidate(tmp_path, "pkg._unused")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

        result = prove_candidate(
            tmp_path,
            cand,
            ProofOptions(run_tests=False, run_typecheck=False),
        )
        assert result.status == ProofStatus.PROVEN
        assert result.removal is not None
        assert result.removal.lines_removed > 0

    def test_working_tree_unchanged_after_prove(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "pkg" / "__init__.py", "")
        orphan = tmp_path / "pkg" / "_orphan.py"
        _write(
            orphan,
            """\
            def dummy():
                return "intact"
            """,
        )
        _commit(tmp_path)

        original_content = orphan.read_text(encoding="utf-8")
        cand = _find_module_candidate(tmp_path, "pkg._orphan")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

        result = prove_candidate(
            tmp_path,
            cand,
            ProofOptions(run_tests=False, run_typecheck=False),
        )
        assert result.status == ProofStatus.PROVEN

        # Working tree file must remain completely untouched
        assert orphan.exists()
        assert orphan.read_text(encoding="utf-8") == original_content

    def test_deletion_that_breaks_pytest_fails(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "pkg" / "__init__.py", "")
        _write(
            tmp_path / "pkg" / "_secret.py",
            """\
            TOKEN = "xyz"
            """,
        )
        # Test checks filesystem existence directly without static import
        _write(
            tmp_path / "tests" / "test_runtime.py",
            """\
            from pathlib import Path
            def test_secret_file_must_exist():
                target = Path(__file__).parent.parent / "pkg" / "_secret.py"
                assert target.exists(), "secret file missing!"
            """,
        )
        _commit(tmp_path)

        cand = _find_module_candidate(tmp_path, "pkg._secret")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

        result = prove_candidate(
            tmp_path,
            cand,
            ProofOptions(run_tests=True, run_typecheck=False),
        )
        assert result.status == ProofStatus.FAILED
        assert result.test_result is not None
        assert result.test_result.status == VerificationStatus.FAILED
        # Working tree preserved
        assert (tmp_path / "pkg" / "_secret.py").exists()

    def test_deletion_that_breaks_mypy_fails(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        _write(tmp_path / "pkg" / "__init__.py", "")
        _write(
            tmp_path / "pkg" / "_secret.py",
            """\
            SECRET = 123
            """,
        )
        _write(tmp_path / "mypy.ini", "[mypy]\n")

        # Create a fake mypy runner that fails if pkg/_secret.py is absent
        fake_mypy = tmp_path / "fake_mypy.sh"
        _write(
            fake_mypy,
            """\
            #!/bin/sh
            if [ ! -f "pkg/_secret.py" ]; then
                echo "error: Module 'pkg._secret' not found" >&2
                exit 1
            fi
            exit 0
            """,
        )
        fake_mypy.chmod(fake_mypy.stat().st_mode | stat.S_IEXEC)
        _commit(tmp_path)

        _orig_which = shutil.which
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: str(fake_mypy) if name == "mypy" else _orig_which(name),
        )

        cand = _find_module_candidate(tmp_path, "pkg._secret")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

        result = prove_candidate(
            tmp_path,
            cand,
            ProofOptions(run_tests=False, run_typecheck=True),
        )
        assert result.status == ProofStatus.FAILED
        assert result.typecheck_result is not None
        assert result.typecheck_result.status == VerificationStatus.FAILED
        assert "not found" in result.typecheck_result.stderr


# ===========================================================================
# CLI Tests for SymbolKind.MODULE
# ===========================================================================


class TestModuleCLI:
    def test_apply_unproven_candidate_blocked(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "pkg" / "__init__.py", "")
        _write(tmp_path / "pkg" / "_orphan.py", "X = 1\n")
        _commit(tmp_path)

        scan_res = runner.invoke(app, ["scan", str(tmp_path)])
        assert scan_res.exit_code == 0

        cand = _find_module_candidate(tmp_path, "pkg._orphan")
        assert cand is not None
        assert cand.classification == SafetyClassification.PROVABLE

        apply_res = runner.invoke(app, ["apply", "DC-001", "--path", str(tmp_path), "--yes"])
        assert apply_res.exit_code == 2
        assert "BLOCKED" in apply_res.output
        assert "has not been proven" in apply_res.output
        assert (tmp_path / "pkg" / "_orphan.py").exists()

    def test_apply_proven_module_deletes_exact_file(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "pkg" / "__init__.py", "")
        orphan = tmp_path / "pkg" / "_orphan.py"
        _write(orphan, "# Line 1\n# Line 2\n# Line 3\n")
        _commit(tmp_path)

        scan_res = runner.invoke(app, ["scan", str(tmp_path)])
        assert scan_res.exit_code == 0

        cand = _find_module_candidate(tmp_path, "pkg._orphan")
        assert cand is not None

        prove_res = runner.invoke(app, ["prove", "DC-001", "--path", str(tmp_path)])
        assert prove_res.exit_code == 0
        assert "PROVEN" in prove_res.output

        # File is still in working tree before apply
        assert orphan.exists()

        apply_res = runner.invoke(app, ["apply", "DC-001", "--path", str(tmp_path), "--yes"])
        assert apply_res.exit_code == 0
        assert "Deleted module file" in apply_res.output
        assert "3 lines" in apply_res.output
        # File is now deleted
        assert not orphan.exists()

    def test_stale_mismatched_proof_blocked_and_file_unchanged(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "pkg" / "__init__.py", "")
        orphan = tmp_path / "pkg" / "_orphan.py"
        _write(orphan, "X = 1\n")
        _commit(tmp_path)

        scan_res = runner.invoke(app, ["scan", str(tmp_path)])
        assert scan_res.exit_code == 0

        # Inject a proof record that points to a different candidate qualified name
        record_proof(
            tmp_path,
            "DC-001",
            {
                "status": "PROVEN",
                "candidate_qualified_name": "pkg.different_module",
            },
        )

        apply_res = runner.invoke(app, ["apply", "DC-001", "--path", str(tmp_path), "--yes"])
        assert apply_res.exit_code == 2
        assert "BLOCKED" in apply_res.output
        assert "does not match candidate" in apply_res.output
        # File must remain untouched
        assert orphan.exists()
